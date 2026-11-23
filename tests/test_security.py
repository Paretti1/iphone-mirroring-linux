"""Regression tests for untrusted network input and local file handling."""
import asyncio
import io
import os
import plistlib
import queue
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av
import numpy as np
from PySide6.QtWidgets import QApplication
from iphone_mirror import net
from iphone_mirror.airplay import Connection, Receiver, Request, MAX_CONNECTIONS, MAX_CONNECTIONS_PER_PEER
from iphone_mirror.config import Settings, private_log
from iphone_mirror.control import usb
from iphone_mirror.control.wda import WDA, WDAError
from iphone_mirror.control import wireless
from iphone_mirror.pairing import PairingSession, ServerIdentity
from iphone_mirror.ui import Bridge, PhoneWindow
from iphone_mirror.video import VideoQueue


def connection(data=b""):
    reader = asyncio.StreamReader(limit=16 * 1024)
    reader.feed_data(data)
    reader.feed_eof()
    writer = Mock()
    writer.get_extra_info.return_value = ("127.0.0.1", 1)
    receiver = SimpleNamespace(identity=ServerIdentity("01" * 32), session_owner=None)
    return Connection(receiver, reader, writer)


class NetworkSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_state_changes_are_rejected(self):
        client = connection()
        for method, path in (("POST", "/fp-setup"), ("SETUP", "/stream"),
                             ("RECORD", "/stream"), ("TEARDOWN", "/stream"),
                             ("SET_PARAMETER", "/stream")):
            response = await client._dispatch(Request(method, path, "RTSP/1.0", {}, b""))
            self.assertEqual(response.status, 403)
            self.assertTrue(response.close)

    async def test_options_remains_available_before_pairing(self):
        response = await connection()._dispatch(Request("OPTIONS", "*", "RTSP/1.0", {}, b""))
        self.assertEqual(response.status, 200)

    async def test_rejects_duplicate_lengths_and_header_injection(self):
        for data in (
            b"POST /pair-setup RTSP/1.0\r\nContent-Length: 0\r\nContent-Length: 4\r\n\r\n",
            b"OPTIONS * RTSP/1.0\r\nCSeq: 1\nInjected: yes\r\n\r\n",
            b"POST /pair-setup RTSP/1.0\r\nContent-Length: -1\r\n\r\n",
            b"POST /pair-setup RTSP/1.0\r\nContent-Length: 1048577\r\n\r\n",
            b"OPTIONS * BAD/1.0\r\n\r\n",
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                await connection(data)._read_request()

    async def test_slow_unauthenticated_peer_is_closed(self):
        client = connection()
        async def stalled():
            await asyncio.sleep(1)
        client._read_request = stalled
        client.cleanup = AsyncMock()
        with patch("iphone_mirror.airplay.HANDSHAKE_TIMEOUT", 0.01):
            await client.serve()
        client.writer.close.assert_called_once()
        client.cleanup.assert_awaited_once()

    async def test_connection_limit_and_phone_allowlist(self):
        writer = Mock()
        writer.get_extra_info.return_value = ("192.0.2.9", 123)
        for tasks, allowed in (
            (dict.fromkeys(range(MAX_CONNECTIONS), "192.0.2.8"), None),
            ({}, "192.0.2.10"),
            (dict.fromkeys(range(MAX_CONNECTIONS_PER_PEER), "192.0.2.9"), None),
        ):
            receiver = SimpleNamespace(_connections=tasks)
            with patch.object(net, "ALLOWED_PHONE", allowed):
                await Receiver._handle(receiver, None, writer)
        self.assertEqual(writer.close.call_count, 3)

    async def test_invalid_setup_does_not_claim_the_receiver(self):
        client = connection()
        client.pairing.verified = True
        body = plistlib.dumps({"streams": [{"type": 110}, {"type": 110}]})
        response = await client._dispatch(Request("SETUP", "/stream", "RTSP/1.0", {}, body))
        self.assertEqual(response.status, 400)
        self.assertTrue(response.close)
        self.assertIsNone(client.rx.session_owner)


class SecurityTests(unittest.TestCase):
    def test_network_volume_values_cannot_overflow_playback_gain(self):
        receiver = SimpleNamespace(volume_db=-10, _apply_volume=Mock())
        for value in (float("nan"), float("inf"), float("-inf")):
            Receiver.set_volume(receiver, value)
            self.assertEqual(receiver.volume_db, -10)
        Receiver.set_volume(receiver, 1e300)
        self.assertEqual(receiver.volume_db, 0)
        Receiver.set_volume(receiver, -1e300)
        self.assertEqual(receiver.volume_db, -144)

    def test_failed_or_restarted_pairing_revokes_verification(self):
        session = PairingSession(ServerIdentity("01" * 32))
        for message in (b"", b"\x01\0\0\0", b"\0\0\0\0", b"\x02\0\0\0"):
            session.verified = True
            with self.assertRaises(ValueError):
                session.pair_verify(message)
            self.assertFalse(session.verified)

    def test_compressed_video_queue_is_bounded_by_bytes_and_count(self):
        packets = VideoQueue(max_bytes=10)
        packets.put_nowait(b"123456")
        with self.assertRaises(queue.Full):
            packets.put_nowait(b"12345")
        self.assertEqual(packets.buffered_bytes, 6)
        self.assertEqual(packets.get_nowait(), b"123456")
        self.assertEqual(packets.buffered_bytes, 0)
        for _ in range(32):
            packets.put_nowait(None)
        with self.assertRaises(queue.Full):
            packets.put_nowait(None)

    def test_ffmpeg_pixel_limit_rejects_oversized_picture(self):
        encoder = av.CodecContext.create("libx264", "w")
        encoder.width = encoder.height = 64
        encoder.pix_fmt = "yuv420p"
        encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
        image = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), np.uint8), format="rgb24")
        packets = encoder.encode(image) + encoder.encode(None)
        data = b"".join(bytes(p) for p in packets)
        self.assertTrue(av.CodecContext.create("h264", "r").decode(av.Packet(data)))
        limited = av.CodecContext.create("h264", "r")
        limited.options = {"max_pixels": "100"}
        with self.assertRaises(av.error.FFmpegError):
            limited.decode(av.Packet(data))

    def test_private_logs_fix_permissions_and_refuse_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log"
            path.write_text("old")
            path.chmod(0o644)
            with private_log(path) as output:
                output.write("new")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            link = Path(tmp) / "symlink"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                private_log(link, "w")
            hardlink = Path(tmp) / "hardlink"
            os.link(path, hardlink)
            with self.assertRaises(OSError):
                private_log(hardlink, "w")
            self.assertEqual(path.read_text(), "oldnew")
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                private_log(fifo)

    def test_wda_response_size_limit(self):
        response = Mock(status=200)
        response.read.return_value = b"x" * 101
        client = WDA("http://127.0.0.1:8100")
        client._conn = Mock()
        client._conn.getresponse.return_value = response
        with patch("iphone_mirror.control.wda.MAX_RESPONSE", 100), self.assertRaises(WDAError):
            client.status()
        response.read.assert_called_once_with(101)
        self.assertIsNone(client._conn)

    def test_wda_url_rejects_ignored_credentials_and_parameters(self):
        for url in ("http://user:pass@127.0.0.1:8100", "http://127.0.0.1:8100/?secret=x",
                    "http://127.0.0.1:8100/#secret"):
            with self.assertRaises(ValueError):
                WDA(url)

    def test_wda_rejects_invalid_session_metadata(self):
        client = WDA("http://127.0.0.1:8100")
        with patch.object(client, "_request", return_value={"sessionId": {"id": "not a string"}}):
            with self.assertRaisesRegex(WDAError, "valid session id"):
                client.new_session()
        with patch.object(client, "_session", return_value={"value": {"width": float("inf"), "height": 390}}):
            with self.assertRaisesRegex(WDAError, "screen size"):
                client.window_size()
        self.assertIsNone(client.size)

    def test_archive_bounds_and_atomic_output(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("WDA.app/binary", b"123456")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "WDA.ipa"
            victim = Path(tmp) / "victim"
            victim.write_bytes(b"keep")
            dest.with_suffix(".ipa.part").symlink_to(victim)
            def fetch(*args, **kwargs):
                return io.BytesIO(archive.getvalue())
            with patch.object(usb.urllib.request, "urlopen", fetch):
                usb.download_wda_ipa(dest, progress=lambda m: None)
                self.assertEqual(victim.read_bytes(), b"keep")
                saved = dest.read_bytes()
                with patch.object(usb, "MAX_MEMBER_BYTES", 5), self.assertRaises(usb.UsbError):
                    usb.download_wda_ipa(dest, progress=lambda m: None)
                self.assertEqual(dest.read_bytes(), saved)
                with patch.object(usb, "MAX_DOWNLOAD", 5), self.assertRaises(usb.UsbError):
                    usb.download_wda_ipa(dest, progress=lambda m: None)

    def test_archive_rejects_link_escape(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            link = zipfile.ZipInfo("WDA.app/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, "../../outside")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            usb.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(archive.getvalue())
        ):
            dest = Path(tmp) / "out.ipa"
            with self.assertRaises(usb.UsbError):
                usb.download_wda_ipa(dest, progress=lambda m: None)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_archive_rejects_files_under_symlink(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            link = zipfile.ZipInfo("WDA.app/framework")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, "bundled-framework")
            output.writestr("WDA.app/framework/info", "file")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            usb.urllib.request, "urlopen", lambda *args, **kw: io.BytesIO(archive.getvalue())
        ):
            with self.assertRaisesRegex(usb.UsbError, "nested under a symlink"):
                usb.download_wda_ipa(Path(tmp) / "out.ipa", progress=lambda message: None)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_viewer_secret_is_not_in_os_process_arguments(self):
        with patch.object(wireless, "dependency_error", return_value=None), \
                patch.object(wireless.subprocess, "call", return_value=0) as call:
            wireless.run_tool(["developer", "core-device", "display", "serve-vnc", "--password", "secret12"])
        self.assertNotIn("secret12", call.call_args.args[0])
        self.assertIn("-I", call.call_args.args[0])
        self.assertEqual(call.call_args.kwargs["env"]["IPHONE_MIRROR_SESSION_PASSWORD"], "secret12")


class OlderIosModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_airplay_preview_keeps_only_the_newest_pending_frame(self):
        frames = []
        bridge = Bridge()
        bridge.frame.connect(lambda *frame: frames.append(frame))
        bridge.post_frame("old", 10, 20)
        bridge.post_frame("new", 20, 40)
        self.assertEqual(frames, [])
        self.app.processEvents()
        self.assertEqual(frames, [("new", 20, 40)])

    def test_direct_viewer_version_failure_selects_airplay(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp) / "settings.json")
            settings["wireless_auto_connect"] = True
            window = PhoneWindow(settings, None)
            try:
                with patch.object(window, "_show_control_error"):
                    window._on_wireless_state("error", wireless.OLDER_IOS_MESSAGE)
                self.assertTrue(settings["legacy_mode"])
                self.assertFalse(settings["wireless_auto_connect"])
                self.assertEqual(window._wireless_button.text(), "AirPlay setup")
            finally:
                window.close()

    def test_legacy_mode_uses_wda_and_explains_airplay(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp) / "settings.json")
            settings["wireless_auto_connect"] = True
            controller = Mock(active=False)
            window = PhoneWindow(settings, None, controller)
            try:
                window._set_legacy_mode(True)
                self.assertFalse(settings["wireless_auto_connect"])
                self.assertEqual(window._wireless_button.text(), "AirPlay setup")
                with patch.object(window.wireless, "start") as start, \
                        patch("iphone_mirror.ui.QMessageBox.information") as info:
                    window.connect_wireless()
                    start.assert_not_called()
                    info.assert_called_once()
                    window._toggle_control()
                    controller.start.assert_called_once()
                self.assertTrue(Settings(settings.path)["legacy_mode"])
                window._set_legacy_mode(False)
                self.assertEqual(window._wireless_button.text(), "Connect over Wi-Fi")
            finally:
                window.wireless.shutdown()
                window.close()

    def test_cli_modes_are_exclusive_and_validate_phone_address(self):
        from iphone_mirror.__main__ import parse_args
        with patch.object(sys, "argv", ["mirror", "--airplay", "--allow-phone", "192.0.2.1"]):
            args = parse_args()
            self.assertTrue(args.airplay)
            self.assertEqual(str(args.allow_phone), "192.0.2.1")
        with patch.object(sys, "argv", ["mirror", "--airplay", "--wireless"]), \
                patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parse_args()


if __name__ == "__main__":
    unittest.main(verbosity=2)
