"""Slow HID transport must not replay a queue of obsolete drag positions."""

import asyncio
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iphone_mirror.wireless_input import LatestMotion, install_low_latency_input


class WirelessInputTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_send_keeps_only_the_latest_pending_move(self):
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(value):
            entered.set()
            await release.wait()
            sent.append(value)

        motion = LatestMotion(send)
        motion.submit((0,))
        await entered.wait()
        for value in range(1, 1001):
            motion.submit((value,))
        self.assertEqual(motion.pending, (1000,))
        release.set()
        await asyncio.wait_for(motion.flush(), 1)
        self.assertEqual(sent, [0, 1000])
        self.assertEqual(motion.coalesced, 999)
        await motion.close()

    async def test_barrier_finishes_drag_before_typing(self):
        events = []

        async def send(value):
            await asyncio.sleep(0.01)
            events.append(value)

        motion = LatestMotion(send)
        motion.submit((1,))
        motion.submit((2,))
        await motion.flush()
        events.append("key")
        motion.submit((3,))
        await motion.flush()
        self.assertEqual(events, [2, "key", 3])
        await motion.close()

    async def test_failed_send_is_reported(self):
        async def fail(*args):
            raise OSError("disconnected")

        motion = LatestMotion(fail)
        motion.submit((1,))
        with self.assertRaises(OSError):
            await motion.flush()
        with self.assertRaises(OSError):
            motion.submit((2,))
        await motion.close()

    def make_rfb_viewer(self):
        from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer, _VncClient

        install_low_latency_input(VncStreamServer)
        viewer = VncStreamServer(SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1", 0))), decoder="av")
        viewer._fb_width, viewer._fb_height = 80, 160
        events = []

        class Hid:
            async def send_touchscreen(self, state, x, y, **kwargs):
                await asyncio.sleep(0.02)
                events.append(("touch", state, x, y))

            async def send_keyboard(self, service, usages):
                await asyncio.sleep(0.02)
                events.append(("key", tuple(usages)))

        async def ready():
            pass

        viewer._ensure_hid = viewer._ensure_keyboard = ready
        viewer._uhs = Hid()
        viewer._touch_service_id, viewer._kb_service_id = 1, 2
        reader = asyncio.StreamReader()
        client = _VncClient(reader, SimpleNamespace())
        return viewer, reader, client, events

    async def test_disconnect_during_drag_releases_the_contact(self):
        from pymobiledevice3.remote.core_device.hid_service import TOUCHSCREEN_STATE_RELEASE

        viewer, reader, client, events = self.make_rfb_viewer()
        reader.feed_data(struct.pack(">BBHH", 5, 1, 5, 10) + struct.pack(">BBHH", 5, 1, 60, 120))
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await asyncio.wait_for(viewer._client_recv_loop(client), timeout=2)
        self.assertFalse(client.pressed)
        self.assertEqual(events[-1][1], TOUCHSCREEN_STATE_RELEASE)
        self.assertIsNone(client._linux_motion.pending)
        self.assertTrue(client._linux_motion.task.done())

    async def test_real_rfb_parser_with_slow_hid_preserves_click_release_and_keys(self):
        viewer, reader, client, events = self.make_rfb_viewer()

        def pointer(mask, x, y):
            return struct.pack(">BBHH", 5, mask, x, y)

        body = bytearray(pointer(1, 5, 10))
        for number in range(1000):
            body += pointer(1, 60, 120)
            if number % 20 == 0:
                body += struct.pack(">BBHHHH", 3, 1, 0, 0, 80, 160)
        body += pointer(0, 60, 120)
        body += struct.pack(">BB2xI", 4, 1, ord("a"))
        body += struct.pack(">BB2xI", 4, 0, ord("a"))
        reader.feed_data(body)
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await asyncio.wait_for(viewer._client_recv_loop(client), timeout=2)
        self.assertEqual([event[0] for event in events], ["touch", "touch", "touch", "key", "key"])
        self.assertLess(events[0][2], events[1][2])
        self.assertEqual(events[1][2:], events[2][2:])
        self.assertFalse(client.pressed)
        self.assertFalse(client.pressed_keys)
        self.assertGreaterEqual(client._linux_motion.coalesced, 990)

    async def test_input_is_prepared_after_video_and_failure_can_retry(self):
        events = []

        class Server:
            async def _start_video(self):
                events.append("video")

            async def _ensure_keyboard(self):
                events.append("input")
                raise OSError("not ready yet")

            async def _handle_pointer(self, *args):
                pass

            async def _handle_key(self, *args):
                pass

            async def _client_recv_loop(self, *args):
                pass

        install_low_latency_input(Server)
        installed = Server._client_recv_loop
        install_low_latency_input(Server)
        self.assertIs(Server._client_recv_loop, installed)
        await Server()._start_video()
        self.assertEqual(events, ["video", "input"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
