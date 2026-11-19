"""iPhone Mirroring Linux desktop application."""
import argparse
import logging
import os
import signal
import sys
from ipaddress import IPv4Address
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import net
from .airplay import Receiver
from .config import APP_ID, APP_NAME, CONFIG_DIR, LOG_FILE, SETTINGS_FILE, Settings
from .control import OFF, Controller
from .ui import Bridge, PhoneWindow, app_icon

INSTANCE_KEY = APP_ID
LOCAL_PORT = 7100  # --local self-tests must not collide with a normal instance on port 7000
LOCAL_SETTINGS = CONFIG_DIR / "settings-local.json"  # ... nor overwrite its settings


def setup_logging():
    LOG_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)


def airplay_port(value):
    port = int(value)
    if not 1024 <= port <= 65535 - net.AUDIO_CONTROL_OFFSET:
        raise argparse.ArgumentTypeError("expected a port from 1024 to 65531")
    return port


def parse_args():
    p = argparse.ArgumentParser(prog="iphone-mirroring-linux")
    p.add_argument(
        "--local",
        action="store_true",
        help=f"self-test: listen on 127.0.0.1:{LOCAL_PORT} only, no mDNS, no single-instance check, "
        f"separate settings file",
    )
    p.add_argument("--port", type=airplay_port, help=f"RTSP port (default {net.PORT}, or {LOCAL_PORT} with --local)")
    p.add_argument("--bind", type=IPv4Address, help="limit AirPlay to one local IPv4 address")
    p.add_argument("--control", action="store_true", help="start iPhone control (USB / WebDriverAgent) at launch")
    p.add_argument("--wda-url", help="use an existing, trusted WebDriverAgent HTTP endpoint")
    p.add_argument("--grab", metavar="DIR", help="self-test: save a PNG of the window every 2 s")
    p.add_argument("--quit-after", type=float, default=0, help="self-test: exit after N seconds")
    return p.parse_args()


def main():
    args = parse_args()
    if args.local:
        net.BIND_HOST = "127.0.0.1"
        net.ADVERTISE = False
        net.PORT = LOCAL_PORT
    if args.bind is not None and not args.local:
        net.BIND_HOST = str(args.bind)
    if args.port:
        net.PORT = args.port
    setup_logging()
    if (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
        and "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        and os.environ.get("DISPLAY")
    ):
        # GNOME's native Wayland windows cannot request always-on-top.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal_timer = QTimer(app, interval=250)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start()

    instance = None
    instance_lock = None
    if not args.local:
        # Taking the lock before removing a stale socket prevents two simultaneous startups.
        runtime = Path(QStandardPaths.writableLocation(QStandardPaths.RuntimeLocation))
        endpoint = str(runtime / INSTANCE_KEY)
        instance_lock = QLockFile(endpoint + ".lock")
        if not instance_lock.tryLock(0):
            probe = QLocalSocket()
            probe.connectToServer(endpoint)
            probe.waitForConnected(300)
            return 0
        QLocalServer.removeServer(endpoint)
        instance = QLocalServer()
        instance.setSocketOptions(QLocalServer.UserAccessOption)
        if not instance.listen(endpoint):
            logging.warning("window activation socket unavailable: %s", instance.errorString())
            instance = None

    settings = Settings(LOCAL_SETTINGS if args.local else SETTINGS_FILE)
    logging.info(
        "starting, name=%r, port=%d, local=%s, pid=%d", settings["name"], net.PORT, args.local, os.getpid()
    )
    bridge = Bridge()
    # Controller.on_state runs on the control thread: hand it to the GUI thread via a queued signal
    ctrl = Controller(
        on_state=lambda state, message: bridge.control.emit(state, message), wda_url=args.wda_url
    )
    win = PhoneWindow(settings, None, ctrl)
    rx = Receiver(
        settings,
        on_frame=lambda arr, w, h: bridge.frame.emit(arr, w, h),
        on_event=lambda e, info: bridge.event.emit(e, info),
    )
    win.rx = rx
    rx.muted = args.local or not settings["audio"]
    bridge.frame.connect(win.on_frame)
    bridge.event.connect(win.on_event)
    bridge.control.connect(win.on_control)
    if instance is not None:
        def show_existing_window():
            connection = instance.nextPendingConnection()
            if connection is not None:
                connection.abort()
                connection.deleteLater()
            win._show_front()

        instance.newConnection.connect(show_existing_window)
    win.show()
    win._update_target()
    rx.start()
    if args.control:
        ctrl.start()
    if args.grab:
        grab_dir = Path(args.grab)
        grab_dir.mkdir(parents=True, exist_ok=True)
        counter = [0]

        def grab():
            counter[0] += 1
            win.grab().save(str(grab_dir / f"window_{counter[0]:02d}.png"))

        timer = QTimer(win, interval=2000, timeout=grab)
        timer.start()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), app.quit)
    code = app.exec()
    settings.save()
    try:
        bridge.control.disconnect(win.on_control)  # the window is going away
    except (RuntimeError, TypeError):
        pass
    if ctrl.state != OFF:
        ctrl.stop()
    rx.stop()
    if instance is not None:
        instance.close()
    if instance_lock is not None:
        instance_lock.unlock()
    logging.info("exit")
    return code


if __name__ == "__main__":
    sys.exit(main())
