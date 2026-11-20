"""Use pymobiledevice3's localhost VNC stream inside the Qt desktop window."""

import logging
import os
import secrets
import socket
import struct
import sys
import time

import numpy as np
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from PySide6.QtCore import QObject, QProcess, QTimer, Qt, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from .control import ERROR, OFF, READY, STARTING
from .control.wireless import LOOPBACK, dependency_error, remote_args

log = logging.getLogger(__name__)

RFB_VERSION = b"RFB 003.008\n"
DESKTOP_SIZE = -223
RAW = 0
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_DESKTOP_NAME = 4096
CONTROL_KEY = 0xFFE3

SPECIAL_KEYS = {
    Qt.Key_Backspace: 0xFF08, Qt.Key_Tab: 0xFF09, Qt.Key_Return: 0xFF0D,
    Qt.Key_Enter: 0xFF8D, Qt.Key_Escape: 0xFF1B, Qt.Key_Home: 0xFF50,
    Qt.Key_Left: 0xFF51, Qt.Key_Up: 0xFF52, Qt.Key_Right: 0xFF53,
    Qt.Key_Down: 0xFF54, Qt.Key_PageUp: 0xFF55, Qt.Key_PageDown: 0xFF56,
    Qt.Key_End: 0xFF57, Qt.Key_Insert: 0xFF63, Qt.Key_Delete: 0xFFFF,
}


def _vnc_auth(password, challenge):
    # RFB security type 2 uses DES with the bits in each password byte reversed.
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in password.encode("ascii"))
    cipher = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return cipher.update(challenge) + cipher.finalize()


class FramebufferClient(QObject):
    """The subset of RFB 3.8 sent by the bundled localhost viewer."""

    frame = Signal(object, int, int)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QTcpSocket(self)
        self.socket.setReadBufferSize(MAX_FRAME_BYTES + 65536)
        self.socket.readyRead.connect(self._read)
        self.socket.connected.connect(
            lambda: self.socket.setSocketOption(QAbstractSocket.LowDelayOption, 1)
        )
        self.drain_timer = QTimer(self, singleShot=True, interval=0)
        self.drain_timer.timeout.connect(self._read)
        self.present_timer = QTimer(self, singleShot=True, interval=16)
        self.present_timer.timeout.connect(self._present)
        self._latest_frame = None
        self.socket.errorOccurred.connect(lambda _: self._fail(self.socket.errorString()))
        self._messages = None
        self._size = 0
        self.width = self.height = 0
        self.received_bytes = self.received_frames = self.presented_frames = 0

    def connect_to(self, port, password):
        self.close()
        self.received_bytes = self.received_frames = self.presented_frames = 0
        self._messages = self._receive(password)
        self._size = next(self._messages)
        self.socket.connectToHost(LOOPBACK, port)

    def close(self):
        self._messages = None
        self.drain_timer.stop()
        self.present_timer.stop()
        self._latest_frame = None
        self.width = self.height = 0
        self.socket.abort()

    def _fail(self, message):
        if self._messages is not None:
            self.close()
            self.failed.emit(message)

    def _read(self):
        consumed = 0
        try:
            while self._messages is not None and self.socket.bytesAvailable() >= self._size:
                packet = bytes(self.socket.read(self._size)) if self._size else b""
                consumed += len(packet)
                self.received_bytes += len(packet)
                required = self._messages.send(packet)
                self._size = required
                if consumed >= 8 * 1024 * 1024:
                    self.drain_timer.start()
                    break
        except (OSError, ValueError, struct.error) as exc:
            self._fail(str(exc))

    def _present(self):
        pending, self._latest_frame = self._latest_frame, None
        if pending is not None:
            self.presented_frames += 1
            self.frame.emit(*pending)

    def _resize(self, width, height):
        if not width or not height or width * height * 4 > MAX_FRAME_BYTES:
            raise ValueError(f"Unsupported iPhone framebuffer size {width} × {height}.")
        self.width, self.height = width, height

    def _request_frame(self, incremental):
        self.socket.write(struct.pack(">BBHHHH", 3, incremental, 0, 0, self.width, self.height))

    def _receive(self, password):
        if (yield 12) != RFB_VERSION:
            raise ValueError("The local wireless viewer did not offer RFB 3.8.")
        self.socket.write(RFB_VERSION)
        count = (yield 1)[0]
        if not count or 2 not in (yield count):
            raise ValueError("The local wireless viewer did not offer password authentication.")
        self.socket.write(b"\x02")
        challenge = yield 16
        self.socket.write(_vnc_auth(password, challenge))
        if (yield 4) != b"\x00" * 4:
            raise ValueError("The local wireless viewer rejected its session password.")
        self.socket.write(b"\x00")  # exclusive client
        init = yield 24
        width, height = struct.unpack_from(">HH", init)
        if init[4:20] != PIXEL_FORMAT:
            raise ValueError("The local wireless viewer sent an unsupported pixel format.")
        self._resize(width, height)
        name_length = struct.unpack_from(">I", init, 20)[0]
        if name_length > MAX_DESKTOP_NAME:
            raise ValueError("The local wireless viewer sent an invalid desktop name.")
        yield name_length
        self.socket.write(struct.pack(">BBHii", 2, 0, 2, RAW, DESKTOP_SIZE))
        self._request_frame(False)

        while True:
            message = (yield 1)[0]
            if message == 2:  # Bell, no payload
                continue
            if message != 0:
                raise ValueError(f"Unexpected wireless viewer message {message}.")
            rectangles = struct.unpack(">xH", (yield 3))[0]
            for _ in range(rectangles):
                x, y, width, height, encoding = struct.unpack(">HHHHi", (yield 12))
                if encoding == DESKTOP_SIZE:
                    self._resize(width, height)
                    continue
                if encoding != RAW or (x, y, width, height) != (0, 0, self.width, self.height):
                    raise ValueError("The local wireless viewer sent an unsupported frame layout.")
                pixels = yield width * height * 4
                frame = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)
                self._latest_frame = (frame, width, height)
                self.received_frames += 1
                if not self.present_timer.isActive():
                    self.present_timer.start()
            self._request_frame(True)

    def pointer(self, x, y, pressed):
        if self.width and self.height:
            px = min(self.width - 1, max(0, round(x * (self.width - 1))))
            py = min(self.height - 1, max(0, round(y * (self.height - 1))))
            self.socket.write(struct.pack(">BBHH", 5, int(pressed), px, py))

    def key_tap(self, keysym, ctrl=False):
        def key(symbol, down):
            return struct.pack(">BB2xI", 4, down, symbol)

        packet = key(keysym, True) + key(keysym, False)
        if ctrl:
            packet = key(CONTROL_KEY, True) + packet + key(CONTROL_KEY, False)
        if self.width and self.height:
            self.socket.write(packet)


class DesktopWireless(QObject):
    """Start and stop the private local bridge and forward its frames to Qt."""

    frame = Signal(object, int, int)
    state_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = OFF
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.client = FramebufferClient(self)
        self.client.frame.connect(self._frame)
        self.client.failed.connect(self._fail)
        self.deadline = QTimer(self, singleShot=True, interval=300000)
        self.deadline.timeout.connect(lambda: self._fail("Timed out waiting for the iPhone. Keep it awake and retry."))
        self.kill_timer = QTimer(self, singleShot=True, interval=5000)
        self.kill_timer.timeout.connect(self.process.kill)
        self.scroll_timer = QTimer(self, interval=20)
        self.scroll_timer.timeout.connect(self._scroll_step)
        self._scroll_points = iter(())
        self._scroll_position = None
        self._phase = ""
        self._pending_output = b""
        self._last_output = ""
        self._port = 0
        self._password = ""
        self._remote = []
        self._device_id = None
        self._retries = 0
        self._stats_started = time.monotonic()
        self._received_frames = 0
        self.retry_timer = QTimer(self, singleShot=True, interval=2000)
        self.retry_timer.timeout.connect(self._retry)
        self.diagnostic_timer = QTimer(self, interval=5000)
        self.diagnostic_timer.timeout.connect(self._diagnose)

    def _diagnose(self):
        log.info(
            "WIRELESS_DIAG desktop phase=%s state=%s bytes=%d received=%d presented=%d "
            "buffered=%d waiting=%d size=%dx%d process=%s",
            self._phase, self.state, self.client.received_bytes, self.client.received_frames,
            self.client.presented_frames, self.client.socket.bytesAvailable(), self.client._size,
            self.client.width, self.client.height, self.process.state().name,
        )

    @property
    def active(self):
        return self.state in (STARTING, READY)

    @property
    def ready(self):
        return self.state == READY

    def _set_state(self, state, message=""):
        self.state = state
        self.state_changed.emit(state, message)

    def start(self, device_id=None, *, retry=False):
        if self.active or self.process.state() != QProcess.NotRunning:
            return
        self._device_id = device_id
        if not retry:
            self._retries = 0
        error = dependency_error()
        if error:
            self._set_state(ERROR, error)
            return

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind((LOOPBACK, 0))
                self._port = listener.getsockname()[1]
        except OSError as exc:
            self._set_state(ERROR, f"Could not reserve a local viewer port. {exc}")
            return
        self._password = secrets.token_hex(4)
        self._remote = remote_args(device_id)
        self._phase = "mount"
        if os.environ.get("IPHONE_MIRROR_DIAGNOSTICS") == "1":
            self.diagnostic_timer.start()
        self.deadline.start(300000)
        self._set_state(STARTING, "Preparing the paired iPhone…")
        self._run(["mounter", "auto-mount", *self._remote])

    def _run(self, args):
        self._pending_output = b""
        self._last_output = ""
        module = "iphone_mirror.wireless_backend" if self._phase == "serve" else "pymobiledevice3"
        # An older checkout in the launch folder must never replace the installed
        # backend. Isolated mode also ignores a stale PYTHONPATH and user site.
        self.process.start(sys.executable, ["-I", "-u", "-m", module, *args])

    def _read_process(self):
        data = self._pending_output + bytes(self.process.readAllStandardOutput())
        lines = data.split(b"\n")
        self._pending_output = lines.pop()[-8192:]
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            log.info("Wireless viewer: %s", line)
            self._last_output = line
            if self.active and self._phase == "serve" and "VNC ready:" in line:
                self._phase = "viewer"
                self.client.connect_to(self._port, self._password)

    def _finished(self, code, status):
        self.kill_timer.stop()
        self._read_process()
        if not self.active:
            return
        if self._phase == "retry":
            self.retry_timer.start()
            return
        if self._phase == "mount" and code == 0 and status == QProcess.NormalExit:
            self._phase = "serve"
            self._stats_started = time.monotonic()
            self._received_frames = 0
            self.deadline.start(45000)
            self._set_state(STARTING, "Connecting to the iPhone display…")
            self._run([
                "developer", "core-device", "display", "serve-vnc",
                "--bind", LOOPBACK, "--port", str(self._port), "--decoder", "av",
                "--password", self._password, *self._remote,
            ])
        else:
            detail = self._pending_output.decode("utf-8", errors="replace").strip() or self._last_output
            self._fail(f"The wireless viewer stopped (exit code {code}). {detail}".strip())

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self._fail(f"Could not start the wireless viewer. {self.process.errorString()}")

    def _frame(self, data, width, height):
        if self.active:
            self._received_frames += 1
            now = time.monotonic()
            elapsed = now - self._stats_started
            if elapsed >= 5:
                log.info("Wireless video received %.1f fps (%dx%d)", self._received_frames / elapsed, width, height)
                self._received_frames = 0
                self._stats_started = now
            if not self.ready:
                self.deadline.stop()
                self._set_state(READY)
            self.frame.emit(data, width, height)

    def _fail(self, message):
        if not self.active or self._phase == "retry":
            return
        log.warning("Wireless connection interrupted: %s", message)
        terminal = any(text in message.lower() for text in (
            "no remotepairing record", "no remote pairing record", "not paired", "unsupported", "authentication"
        ))
        if self._retries >= 3 or terminal:
            self._stop(ERROR, message)
            return
        self._retries += 1
        self._phase = "retry"
        self._stop(STARTING, f"Reconnecting over Wi-Fi… (attempt {self._retries}/3)")
        if self.process.state() == QProcess.NotRunning:
            self.retry_timer.start()

    def _retry(self):
        if not self.active:
            return
        if self.process.state() != QProcess.NotRunning:
            self.retry_timer.start()
            return
        self.state = OFF
        self.start(self._device_id, retry=True)

    def _stop(self, state, message=""):
        self.diagnostic_timer.stop()
        self.deadline.stop()
        self.retry_timer.stop()
        self._cancel_scroll()
        self.client.close()
        self._set_state(state, message)
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            self.kill_timer.start()

    def stop(self):
        if self.active:
            self._stop(OFF)

    def shutdown(self):
        self.stop()
        if self.process.state() != QProcess.NotRunning and not self.process.waitForFinished(2000):
            self.process.kill()
            self.process.waitForFinished(1000)

    def touch(self, x, y, pressed):
        self._cancel_scroll()
        self.client.pointer(x, y, pressed)

    def scroll(self, x, y, notches):
        self._cancel_scroll()
        # A vertical touch swipe works with apps that expect finger gestures.
        distance = min(0.65, max(0.18, abs(notches) * 0.26))
        if notches < 0:
            start = min(0.93, max(y, 0.07 + distance))
            end = start - distance
        else:
            start = max(0.07, min(y, 0.93 - distance))
            end = start + distance
        self._scroll_position = (x, start)
        self._scroll_points = ((x, start + (end - start) * step / 5) for step in range(1, 6))
        self.client.pointer(x, start, True)
        self.scroll_timer.start()

    def _scroll_step(self):
        point = next(self._scroll_points, None)
        if point is None:
            self._cancel_scroll()
        else:
            self._scroll_position = point
            self.client.pointer(*point, True)

    def _cancel_scroll(self):
        if self._scroll_position is not None:
            self.scroll_timer.stop()
            self.client.pointer(*self._scroll_position, False)
            self._scroll_position = None

    def key(self, event):
        keysym = SPECIAL_KEYS.get(event.key())
        text = event.text()
        if keysym is None and len(text) == 1 and 32 <= ord(text) <= 126:
            keysym = ord(text)
        if keysym is not None:
            self.client.key_tap(keysym)
            return True
        return False

    def phone_button(self, method, *args):
        symbol = {"home": "h", "volumeUp": "]", "volumeDown": "["}.get(args[0] if args else method)
        if symbol:
            self.client.key_tap(ord(symbol), ctrl=True)
            return True
        return False
