"""Real HEVC packets through the same RTP receiver used by the desktop app."""

import asyncio
import re
import struct
import sys
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer
from iphone_mirror.wireless_backend import (
    diagnostic_receiver, diagnostic_transcoder, request_software_keyframe, FrameMailbox,
)
from iphone_mirror.realtime_hevc import MAX_QUEUED_PACKETS, RealtimeHevcDecoder, preview_edge


def encoded_frames(count=20, width=160, height=320):
    encoder = av.CodecContext.create("libx265", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 60)
    encoder.framerate = Fraction(60, 1)
    encoder.options = {
        "preset": "ultrafast", "tune": "zerolatency",
        "x265-params": "log-level=error:keyint=1000:min-keyint=1000:scenecut=0:bframes=0",
    }
    packets = []
    for number in range(count):
        pixels = np.full((height, width, 3), (number * 10) % 256, np.uint8)
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


def create_decoder(decoder_class, packet, on_frame, on_error):
    nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", packet) if nal]
    parameter_sets = {((nal[0] >> 1) & 0x3F): nal for nal in nals}
    return decoder_class(*(parameter_sets[kind] for kind in (32, 33, 34)),
                         on_frame=on_frame, on_decode_error=on_error)


class RealtimeDecoderTest(unittest.TestCase):
    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for decoder")
            time.sleep(0.005)

    def test_slow_presentation_does_not_queue_predictive_video(self):
        packets = encoded_frames(count=120)
        received, errors = [], []

        def slow_display(pixels):
            time.sleep(0.04)  # Deliberately cap display conversion/delivery at 25 fps.
            received.append(pixels[0])

        decoder = create_decoder(RealtimeHevcDecoder, packets[0], slow_display, lambda: errors.append(True))
        started = time.monotonic()
        try:
            for index, packet in enumerate(packets):
                decoder.feed(packet)
                time.sleep(max(0, started + (index + 1) / 60 - time.monotonic()))
                self.assertLessEqual(decoder._inq.qsize(), MAX_QUEUED_PACKETS)
            self.wait_for(lambda: decoder.decoded_frames == len(packets))
            last_value = ((len(packets) - 1) * 10) % 256
            self.wait_for(lambda: received and abs(received[-1] - last_value) <= 3, timeout=0.3)
            self.assertLess(time.monotonic() - started, 2.3)
            self.assertGreater(decoder.skipped_previews, 20)
            self.assertLess(len(received), len(packets))
            self.assertEqual(decoder.dropped_packets, 0)
            self.assertEqual(errors, [])
            self.assertEqual(decoder._inq.qsize(), 0)
        finally:
            decoder.close()
        self.assertFalse(decoder._thread.is_alive())
        self.assertFalse(decoder._present_thread.is_alive())

    def test_overloaded_decode_waits_for_a_keyframe_with_bounded_memory(self):
        packets = encoded_frames(count=30)
        blocked, release = threading.Event(), threading.Event()

        class SlowDecoder(RealtimeHevcDecoder):
            def _new_codec(self):
                if not blocked.is_set():
                    blocked.set()
                    release.wait(3)
                return super()._new_codec()

        received, errors = [], []
        decoder = create_decoder(SlowDecoder, packets[0], received.append, lambda: errors.append(True))
        try:
            decoder.feed(packets[0])
            self.assertTrue(blocked.wait(1))
            for packet in packets[1:]:
                decoder.feed(packet)
                self.assertLessEqual(decoder._inq.qsize(), MAX_QUEUED_PACKETS)
            self.assertTrue(decoder._waiting_for_keyframe)
            self.assertGreater(decoder.dropped_packets, 0)
            self.assertTrue(errors)
            # Restart from an IDR, never a delta frame from the abandoned chain.
            decoder.feed(packets[0])
            self.assertFalse(decoder._waiting_for_keyframe)
            release.set()
            self.wait_for(lambda: len(received) >= 1)
            for packet in packets[1:5]:
                decoder.feed(packet)
                time.sleep(0.02)
            self.wait_for(lambda: received and abs(received[-1][0] - 40) <= 3)
            self.assertEqual(decoder.recoveries, 1)
        finally:
            release.set()
            decoder.close()

    @patch.dict("os.environ", {"IPHONE_MIRROR_PREVIEW_EDGE": "1080"})
    def test_resize_happens_before_bgra_packing(self):
        packets = encoded_frames(count=1, width=1184, height=2560)
        received = []
        decoder = create_decoder(RealtimeHevcDecoder, packets[0], received.append, lambda: None)
        try:
            decoder.feed(packets[0])
            self.wait_for(lambda: received)
            self.assertEqual(max(decoder.width, decoder.height), 1080)
            self.assertEqual(len(received[0]), decoder.width * decoder.height * 4)
            self.assertTrue(np.all(np.frombuffer(received[0], np.uint8)[3::4] == 255))
        finally:
            decoder.close()

    def test_quality_choices_are_bounded(self):
        for setting, expected in (("720", 720), ("1080", 1080), ("1440", 1440), ("bad", 1080), ("99999", 1080)):
            with patch.dict("os.environ", {"IPHONE_MIRROR_PREVIEW_EDGE": setting}):
                self.assertEqual(preview_edge(), expected)


class WirelessVideoTest(unittest.IsolatedAsyncioTestCase):
    async def test_display_loop_keeps_only_the_newest_notification(self):
        published = []
        mailbox = FrameMailbox(asyncio.get_running_loop(), lambda *frame: published.append(frame))
        for number in range(1000):
            mailbox.submit((bytes([number % 256]), 1, 1, 0))
        self.assertEqual(published, [])
        await asyncio.sleep(0)
        self.assertEqual(published, [(bytes([999 % 256]), 1, 1, 0)])
        self.assertFalse(mailbox.scheduled)
        self.assertIsNone(mailbox.pending)

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

    async def test_diagnostics_preserve_video_and_report_each_stage(self):
        receive = diagnostic_receiver(VncStreamServer._udp_recv_and_pipe)
        create = diagnostic_transcoder(VncStreamServer._new_transcoder)
        with (
            patch.object(VncStreamServer, "_fire_decoder_refresh", request_software_keyframe),
            patch.object(VncStreamServer, "_udp_recv_and_pipe", receive),
            patch.object(VncStreamServer, "_new_transcoder", create),
            self.assertLogs("iphone_mirror.wireless_backend", level="INFO") as records,
        ):
            await self.exercise(refresh=False)
        final = next(line for line in records.output if "ingress_stopped" in line)
        for counter in ("fed=20", "decoded=20", "published=20", "decode_errors=0", "queued=0"):
            self.assertIn(counter, final)

    async def test_diagnostics_report_even_when_no_packets_arrive(self):
        viewer = VncStreamServer(SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1", 0))), decoder="av")
        with self.assertLogs("iphone_mirror.wireless_backend", level="INFO") as records:
            task = asyncio.create_task(diagnostic_receiver(VncStreamServer._udp_recv_and_pipe)(viewer, Transport()))
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any("backend packets=0 bytes=0" in line for line in records.output))
        self.assertTrue(any("ingress_stopped" in line for line in records.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
