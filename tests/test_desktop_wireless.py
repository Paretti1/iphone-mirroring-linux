"""Exercise the installed desktop window with pymobiledevice3's local VNC protocol."""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import av
import numpy as np
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from iphone_mirror.config import Settings
from iphone_mirror.ui import PhoneWindow
from iphone_mirror.wireless_backend import emit_packed_frame, present_desktop_frame

SIMULATED_VIEWER = """\
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

async def main():
    port, password, events = sys.argv[1:]
    records = Path(events)
    rsd = SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1", 0)))
    viewer = VncStreamServer(rsd, decoder="av", password=password)
    viewer._broadcast_frame(bytes((20, 40, 80, 255)) * (80 * 160), 80, 160, 0)

    async def pointer(client, mask, x, y):
        with records.open("a") as out:
            print("pointer", mask, x, y, file=out)

    async def key(client, down, keysym):
        with records.open("a") as out:
            print("key", int(down), keysym, file=out)
        if not down and keysym == ord("a"):
            viewer._broadcast_frame(bytes((80, 40, 20, 255)) * (160 * 80), 160, 80, 0)

    viewer._handle_pointer = pointer
    viewer._handle_key = key
    async with await asyncio.start_server(viewer._handle_client, "127.0.0.1", int(port)) as listener:
        print("VNC ready:", flush=True)
        await listener.serve_forever()

asyncio.run(main())
"""


HEVC_VIEWER = """
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[4])
from test_wireless_video import Transport, encoded_frames
from iphone_mirror.wireless_backend import configure_backend
configure_backend()
from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

async def main():
    port, password, event_path = sys.argv[1:4]
    records = Path(event_path)
    viewer = VncStreamServer(SimpleNamespace(service=SimpleNamespace(address=("127.0.0.1",0))),
                             decoder="av", password=password)
    viewer._loop = asyncio.get_running_loop()
    # A real sender advertises an RTCP destination. This activates the problematic
    # refresh on client-connect in the unpatched upstream server.
    viewer._rtcp_dest = ("127.0.0.1", 9)
    transport = Transport()
    packets = encoded_frames()
    receive = asyncio.create_task(viewer._udp_recv_and_pipe(transport))
    transport.feed(packets[0])
    while not viewer._ready.is_set():
        await asyncio.sleep(0.01)

    async def pointer(client, mask, x, y):
        with records.open("a") as output:
            print("pointer", mask, file=output)

    async def key(client, down, code):
        with records.open("a") as output:
            print("key", int(down), code, file=output)

    viewer._handle_pointer = pointer
    viewer._handle_key = key

    async def feed():
        while not viewer._clients:
            await asyncio.sleep(0.01)
        for packet in packets[1:]:
            transport.feed(packet)
            await asyncio.sleep(0.05)

    feed_task = asyncio.create_task(feed())
    try:
        async with await asyncio.start_server(viewer._handle_client, "127.0.0.1", int(port)) as server:
            print("VNC ready:", flush=True)
            await server.serve_forever()
    finally:
        receive.cancel()
        feed_task.cancel()
        await asyncio.gather(receive, feed_task, return_exceptions=True)
        if viewer._transcoder:
            viewer._transcoder.close()

asyncio.run(main())
"""


class PackedFrameTest(unittest.TestCase):
    def test_desktop_frame_rotation_and_transport_size(self):
        pixels = np.zeros((2556, 1179, 4), dtype=np.uint8)
        pixels[:, :, 3] = 255
        for rotation in (0, 90, 180, 270):
            viewer = SimpleNamespace(_rotation=rotation, _display_size=(1179, 2556))
            data, width, height, angle = present_desktop_frame(viewer, pixels.tobytes(), 1179, 2556)
            self.assertLessEqual(max(width, height), 1440)
            self.assertEqual(len(data), width * height * 4)
            self.assertEqual(angle, rotation)
            self.assertEqual(width < height, rotation in (0, 180))

    def test_iphone_width_removes_ffmpeg_stride_padding(self):
        for width in (1080, 1179, 1180):
            with self.subTest(width=width):
                images = []
                target = SimpleNamespace(width=width, height=240, _on_frame=images.append)
                for value in (20, 100):
                    source = av.VideoFrame.from_ndarray(
                        np.full((240, width, 3), value, dtype=np.uint8), format="rgb24"
                    )
                    emit_packed_frame(target, source)
                self.assertEqual(len(images[0]), width * 240 * 4)
                self.assertEqual(len(images[1]), width * 240 * 4)
                first = np.frombuffer(images[0], np.uint8).reshape(240, width, 4)
                last = np.frombuffer(images[1], np.uint8).reshape(240, width, 4)
                self.assertTrue(np.all(first[:, :, :3] == 20))
                self.assertTrue(np.all(last[:, :, :3] == 100))
                self.assertTrue(np.all(last[:, :, 3] == 255))


@unittest.skipUnless(importlib.util.find_spec("pymobiledevice3"), "Wireless control dependencies are not installed")
class DesktopWirelessTest(unittest.TestCase):
    def wait_for(self, predicate, message, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            QTest.qWait(20)
        self.fail(message)

    @patch.dict(os.environ, {"IPHONE_MIRROR_DIAGNOSTICS": "1"})
    def test_hevc_video_keeps_updating_while_input_is_sent(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            window = PhoneWindow(Settings(path / "config.json"), None)
            backend = window.wireless
            received = []
            backend.frame.connect(lambda data, width, height: received.append(int(data[0, 0, 0])))
            original_start = backend.process.start
            commands = []
            events = path / "events.txt"

            def start(program, args):
                commands.append(args)
                if "mounter" in args:
                    original_start(sys.executable, ["-c", "pass"])
                else:
                    self.assertIn("iphone_mirror.wireless_backend", args)
                    original_start(sys.executable, [
                        "-u", "-c", HEVC_VIEWER, args[args.index("--port") + 1],
                        args[args.index("--password") + 1], str(events), str(Path(__file__).parent),
                    ])

            window.show()
            try:
                with patch.object(backend.process, "start", side_effect=start):
                    QTest.mouseClick(window._wireless_button, Qt.LeftButton)
                    self.wait_for(lambda: len(received) >= 1, "The first HEVC frame did not arrive")
                    QTest.mouseClick(window, Qt.LeftButton, pos=window.phone_rect().center().toPoint())
                    QTest.keyClicks(window, "hello")
                    self.wait_for(lambda: len(received) >= 15, "HEVC video froze after initial display")
                    self.assertGreater(len(set(received)), 10)
                    self.assertEqual(len(commands), 2, "Input should not restart the video session")
                    self.assertIn("pointer", events.read_text())
                    self.assertIn("key", events.read_text())
                    self.assertTrue(backend.ready)
                    self.assertTrue(backend.diagnostic_timer.isActive())
                    # The old input watchdog disconnected a healthy session after eight
                    # seconds with no further video. Let the encoded sequence finish,
                    # send input, and ensure that static pictures keep the session alive.
                    QTest.qWait(1200)
                    QTest.keyClicks(window, " again")
                    QTest.qWait(8500)
                    self.assertTrue(backend.ready)
                    self.assertEqual(len(commands), 2, "A static picture triggered a reconnect")
                    with self.assertLogs("iphone_mirror.desktop_wireless", level="INFO") as logs:
                        backend._diagnose()
                    self.assertIn("WIRELESS_DIAG desktop phase=viewer state=ready", logs.output[0])
                    self.assertIn(f"presented={len(received)}", logs.output[0])
            finally:
                backend.shutdown()
                window.close()
                app.processEvents()

    def test_toolbar_at_small_sizes_and_control_states(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            window = PhoneWindow(Settings(Path(directory) / "settings.json"), None)
            window.show()
            try:
                for aspect in (0.46, 2.16):
                    window.aspect = aspect
                    for height in (160, 320, 600):
                        window.set_phone_height(height)
                        app.processEvents()
                        toolbar = window._toolbar
                        buttons = list(window._toolbar_buttons.values())
                        self.assertTrue(toolbar.isVisible())
                        for index, button in enumerate(buttons):
                            self.assertTrue(button.isVisible())
                            self.assertGreaterEqual(button.width(), 32)
                            if index:
                                self.assertGreater(button.x(), buttons[index - 1].geometry().right())
                        self.assertFalse(window._toolbar_buttons["home"].isEnabled())
                        previous = window.settings["always_on_top"]
                        QTest.mouseClick(window._toolbar_buttons["pin"], Qt.LeftButton)
                        self.assertEqual(window.settings["always_on_top"], not previous)
                window.aspect = 0.46
                window.set_phone_height(600)
                app.processEvents()
                window.grab().save("/tmp/iphone-toolbar-review.png")
            finally:
                window.wireless.shutdown()
                window.close()

    def test_desktop_connects_displays_controls_and_disconnects(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            events = tmp / "input.txt"
            window = PhoneWindow(Settings(tmp / "settings.json"), None, wireless_device="PAIRED-ID")
            bridge = window.wireless
            launch = bridge.process.start
            launches = []

            def simulate(_program, args):
                launches.append(args)
                if "mounter" in args:
                    launch(sys.executable, ["-c", "pass"])
                else:
                    self.assertIn("--remotepairing", args)
                    self.assertEqual(args[args.index("--udid") + 1], "PAIRED-ID")
                    self.assertEqual(args[args.index("--bind") + 1], "127.0.0.1")
                    self.assertEqual(args[args.index("--decoder") + 1], "av")
                    launch(
                        sys.executable,
                        ["-u", "-c", SIMULATED_VIEWER, args[args.index("--port") + 1],
                         args[args.index("--password") + 1], str(events)],
                    )

            window.show()
            try:
                with patch.object(bridge.process, "start", side_effect=simulate):
                    QTest.mouseClick(window._wireless_button, Qt.LeftButton)
                    self.wait_for(
                        lambda: window.src_size == (80, 160),
                        f"Desktop stream did not start. {bridge._last_output}",
                    )
                    self.assertTrue(bridge.ready)
                    self.assertEqual(len(launches), 2)
                    self.assertEqual(window.image.pixelColor(0, 0).red(), 80)

                    QTest.mouseClick(window, Qt.LeftButton, pos=window.phone_rect().center().toPoint())
                    QTest.keyClicks(window, "a")
                    self.wait_for(lambda: window.src_size == (160, 80), "Desktop did not follow the resized stream")
                    recorded = events.read_text().splitlines()
                    self.assertEqual(len(recorded), 4)
                    press, release, key_down, key_up = recorded
                    self.assertTrue(press.startswith("pointer 1 "), press)
                    self.assertTrue(release.startswith("pointer 0 "), release)
                    self.assertEqual((key_down, key_up), ("key 1 97", "key 0 97"))
                    self.assertEqual(window.image.pixelColor(0, 0).red(), 20)

                    center = window.phone_rect().center()
                    wheel = QWheelEvent(
                        center, QPointF(window.mapToGlobal(center.toPoint())), QPoint(), QPoint(0, -120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
                    )
                    QApplication.sendEvent(window, wheel)
                    self.wait_for(
                        lambda: len(events.read_text().splitlines()) >= 11,
                        "Mouse wheel did not send a complete swipe",
                    )
                    swipe = [row.split() for row in events.read_text().splitlines()[4:]]
                    self.assertEqual([row[1] for row in swipe], ["1"] * 6 + ["0"])
                    self.assertLess(int(swipe[-1][3]), int(swipe[0][3]))

                    old_process = bridge.process.processId()
                    # The server remains alive and accepts input, but stops sending pictures.
                    bridge.touch(0.5, 0.5, True)
                    bridge.touch(0.5, 0.5, False)
                    QTest.qWait(150)
                    self.assertTrue(bridge.ready)
                    self.assertEqual(bridge.process.processId(), old_process)
                    bridge.process.kill()
                    self.wait_for(
                        lambda: bridge.ready and bridge.process.processId() != old_process,
                        "Desktop did not reconnect after the viewer exited", timeout=10,
                    )
                    self.assertTrue(window.settings.get("wireless_auto_connect"))
                    self.assertEqual(window.settings.get("wireless_device"), "PAIRED-ID")
                    self.assertTrue(window._toolbar_buttons["home"].isEnabled())
                    window.disconnect_wireless()
                    self.assertFalse(window.settings.get("wireless_auto_connect"))
                    self.wait_for(lambda: not bridge.process.processId(), "Local viewer did not stop")
                    self.assertTrue(window._wireless_button.isVisible())
                    self.assertIsNone(window.image)
            finally:
                bridge.shutdown()
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
