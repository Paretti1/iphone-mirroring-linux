"""Real HEVC packets through the same RTP receiver used by the desktop app."""

import asyncio
import re
import struct
import sys
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer
from iphone_mirror.wireless_backend import request_software_keyframe


def encoded_frames():
    encoder = av.CodecContext.create("libx265", "w")
    encoder.width, encoder.height = 160, 320
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 60)
    encoder.framerate = Fraction(60, 1)
    encoder.options = {
        "preset": "ultrafast", "tune": "zerolatency",
        "x265-params": "log-level=error:keyint=1000:min-keyint=1000:scenecut=0:bframes=0",
    }
    packets = []
    for number in range(20):
        pixels = np.full((320, 160, 3), number * 10, np.uint8)
        image = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        image.pts = number
        packets.extend(bytes(packet) for packet in encoder.encode(image))
    packets.extend(bytes(packet) for packet in encoder.encode(None))
    return packets


class Transport:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sequence = 0

    async def recv(self):
        return await self.queue.get()

    def feed(self, packet):
        nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", packet) if nal]
        for index, nal in enumerate(nals):
            marker = 0x80 if index == len(nals) - 1 else 0
            header = struct.pack(">BBHII", 0x80, 96 | marker, self.sequence, 0, 1)
            self.sequence += 1
            self.queue.put_nowait(header + nal)


class WirelessVideoTest(unittest.IsolatedAsyncioTestCase):
    async def wait_frames(self, viewer, count):
        for _ in range(150):
            if viewer._frames_emitted >= count:
                return
            await asyncio.sleep(0.02)
        self.fail(f"Expected {count} decoded frames, received {viewer._frames_emitted}")

    async def exercise(self, refresh):
        rsd = SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1", 0)))
        viewer = VncStreamServer(rsd, decoder="av")
        viewer._loop = asyncio.get_running_loop()
        transport = Transport()
        task = asyncio.create_task(viewer._udp_recv_and_pipe(transport))
        packets = encoded_frames()
        try:
            transport.feed(packets[0])
            await self.wait_frames(viewer, 1)
            viewer._fire_decoder_refresh(viewer._loop.time(), reason="client-connect")
            self.assertEqual(viewer._refresh_pending, refresh)
            for packet in packets[1:]:
                transport.feed(packet)
                await asyncio.sleep(1 / 60)
            if refresh:
                await asyncio.sleep(0.1)
                self.assertEqual(viewer._frames_emitted, 1)
            else:
                await self.wait_frames(viewer, len(packets))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if viewer._transcoder:
                viewer._transcoder.close()
            await asyncio.gather(*viewer._pli_tasks, return_exceptions=True)

    async def test_linux_video_continues_after_viewer_connects(self):
        with patch.object(VncStreamServer, "_fire_decoder_refresh", request_software_keyframe):
            await self.exercise(refresh=False)

    async def test_linux_recovery_does_not_gate_video(self):
        with patch.object(VncStreamServer, "_fire_decoder_refresh", request_software_keyframe):
            rsd = SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1", 0)))
            viewer = VncStreamServer(rsd, decoder="av")
            for reason in ("client-connect", "active", "settled", "heartbeat", "decode-error", "re-arm"):
                viewer._fire_decoder_refresh(asyncio.get_running_loop().time(), reason=reason)
                self.assertFalse(viewer._refresh_pending)
            await asyncio.gather(*viewer._pli_tasks, return_exceptions=True)

    async def test_legacy_refresh_stops_video_after_first_image(self):
        await self.exercise(refresh=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
