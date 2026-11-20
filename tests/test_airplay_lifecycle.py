"""Exercise discovery and shutdown after Qt initializes, including on Python 3.15."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication
from iphone_mirror.airplay import Receiver
from iphone_mirror.config import Settings
from iphone_mirror import net


class AirplayLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_discovery_start_and_stop_after_qt_initialization(self):
        calls, events = [], []
        announced = threading.Event()

        class Advertiser:
            addresses = ["127.0.0.1"]

            def __init__(self, *args):
                pass

            def start(self):
                calls.append(("start", threading.current_thread().name))

            def stop(self):
                calls.append(("stop", threading.current_thread().name))

        def event(kind, info):
            events.append(kind)
            if kind in ("listening", "error"):
                announced.set()

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(net, "ADVERTISE", True),
            patch.object(net, "BIND_HOST", "127.0.0.1"),
            patch.object(net, "PORT", 0),
            patch("iphone_mirror.airplay.discovery.Advertiser", Advertiser),
        ):
            receiver = Receiver(Settings(Path(directory) / "settings.json"), lambda *args: None, event)
            try:
                receiver.start()
                self.assertTrue(announced.wait(5), "Discovery did not finish")
                self.assertEqual(events, ["listening"], "Discovery failed after Qt initialized")
            finally:
                receiver.stop()
            self.assertFalse(receiver._thread.is_alive(), "Receiver failed to stop")
            self.assertEqual([kind for kind, _ in calls], ["stop", "start", "stop"])
            self.assertTrue(all(name != receiver._thread.name for _, name in calls),
                            "Discovery blocked the AirPlay event-loop thread")


if __name__ == "__main__":
    unittest.main(verbosity=2)
