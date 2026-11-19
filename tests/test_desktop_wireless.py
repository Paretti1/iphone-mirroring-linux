"""Exercise the installed desktop window with pymobiledevice3's local VNC protocol."""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
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


@unittest.skipUnless(importlib.util.find_spec("pymobiledevice3"), "Wireless control dependencies are not installed")
class DesktopWirelessTest(unittest.TestCase):
    def wait_for(self, predicate, message, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            QTest.qWait(20)
        self.fail(message)

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

                    bridge.stop()
                    self.wait_for(lambda: not bridge.process.processId(), "Local viewer did not stop")
                    self.assertTrue(window._wireless_button.isVisible())
                    self.assertIsNone(window.image)
            finally:
                bridge.shutdown()
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
