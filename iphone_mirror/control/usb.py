"""Bring up WebDriverAgent on a USB-connected iPhone with pymobiledevice3.

Steps (each is a pymobiledevice3 CLI call, run in the background without a console):
  1. find the trusted iPhone through the system usbmuxd
  2. check Developer Mode
  3. mount the Developer Disk Image (downloaded/personalised automatically)
  4. find the installed WebDriverAgent runner and launch it as an XCUITest
     (iOS 17.4+: through a userspace tunnel, no admin rights needed)
  5. forward a local TCP port to WDA's port 8100 on the phone
"""
import importlib.util
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from ..config import APP_DIR, downloads_dir
from .wda import WDA, WDAError, describe_error

log = logging.getLogger(__name__)

WDA_PORT = 8100
WDA_RELEASE_URL = "https://github.com/appium/WebDriverAgent/releases/latest/download/WebDriverAgentRunner-Runner.zip"

MSG_NO_USBMUXD = (
    "The system usbmuxd service is unavailable.\n"
    "Install usbmuxd for your distribution, connect the iPhone by USB, unlock it and tap Trust.\n"
    "Check the service with: systemctl status usbmuxd"
)
MSG_MULTIPLE_DEVICES = "Multiple iPhones are connected by USB. Leave only the iPhone you want to control connected."
MSG_NO_DEVICE = "No iPhone found on USB. Connect it with a cable, unlock it and tap \"Trust\"."
MSG_NOT_TRUSTED = (
    "The iPhone does not trust this PC yet. Unlock it, tap \"Trust\" and enter the passcode, then try again."
)
MSG_DENIED = "The iPhone refused to trust this PC. Unplug it, plug it back in and tap \"Trust\"."
MSG_LOCKED = "The iPhone is locked. Unlock it and try again."
MSG_NO_PMD3 = "pymobiledevice3 is not installed. Run ./install.sh --usb from the source checkout."
MSG_DEV_MODE = (
    "Developer Mode is off.\n"
    "Run:  python -m iphone_mirror.control reveal-dev-mode\n"
    "then on the iPhone: Settings > Privacy & Security > Developer Mode > On (the phone restarts)."
)
MSG_NO_WDA = (
    "WebDriverAgent is not installed on the iPhone.\n"
    "Run:  python -m iphone_mirror.control get-wda\n"
    "and sign and install the resulting WebDriverAgentRunner.ipa "
    "(see README, section \"USB control\")."
)
MSG_UNTRUSTED_DEV = (
    "The iPhone does not trust the developer of WebDriverAgent yet.\n"
    "On the iPhone open Settings > General > VPN & Device Management, tap your Apple ID, "
    "tap \"Trust\", then try again."
)
MSG_EXPIRED = (
    "The WebDriverAgent signature has expired (apps signed with a free Apple ID last 7 days).\n"
    "Re-sign and install WebDriverAgentRunner.ipa again."
)
MSG_IOS_17_EARLY = (
    "iOS 17.0-17.3 can only be controlled with administrator rights.\n"
    "Update the iPhone to iOS 17.4 or later, or run the pymobiledevice3 tunneld service as root first."
)
MSG_WDA_TIMEOUT = (
    "WebDriverAgent did not start. If the phone shows an \"Untrusted Developer\" message, trust it in "
    "Settings > General > VPN & Device Management, then try again."
)

# known pymobiledevice3 / XCTest error texts -> advice for the user (checked in order)
_HINTS = [
    (("connect to usbmuxd",), MSG_NO_USBMUXD),
    (("password protected", "unlock and retry"), MSG_LOCKED),
    (("refused to trust",), MSG_DENIED),
    (("trust dialog", "not paired", "does not trust this computer", "pairing with the device failed"), MSG_NOT_TRUSTED),
    (("developer mode is disabled",), MSG_DEV_MODE),
    (("cannot serve this device", "tunneld"), MSG_IOS_17_EARLY),
    (("explicitly trusted", "untrusted developer", "invalid code signature"), MSG_UNTRUSTED_DEV),
    (("has expired",), MSG_EXPIRED),
    (("device is not connected", "no device with the requested udid"), MSG_NO_DEVICE),
]


class UsbError(Exception):
    pass


class Cancelled(UsbError):
    """The USB session was closed while it was being brought up."""

    def __init__(self, message="cancelled"):
        super().__init__(message)


# ---------------------------------------------------------------------- child processes
def _child_env():
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
    return env


def _popen(args, **kw):
    if importlib.util.find_spec("pymobiledevice3") is None:
        raise UsbError(MSG_NO_PMD3)
    return subprocess.Popen(
        [sys.executable, "-m", "pymobiledevice3", *args],
        stdin=subprocess.DEVNULL, env=_child_env(), **kw,
    )


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass


def pmd3(*args, timeout=60, cancel=None):
    """Run a pymobiledevice3 command; returns (exit code, stdout, stderr).
    `cancel` (a threading.Event) kills the command early and raises Cancelled."""
    log.info("pymobiledevice3 %s", " ".join(args))
    try:
        p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise UsbError(f"Could not start pymobiledevice3: {describe_error(e)}") from None
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or time.monotonic() > deadline:
                _kill(p)
                p.communicate()
                if cancelled:
                    raise Cancelled() from None
                raise UsbError(f"pymobiledevice3 {' '.join(args)} timed out") from None
    if p.returncode:
        if "No module named pymobiledevice3" in err:
            raise UsbError(MSG_NO_PMD3)
        log.warning("pymobiledevice3 %s failed (exit %s): %s", " ".join(args[:3]), p.returncode, _tail(err, 3))
    return p.returncode, out, err


# ---------------------------------------------------------------------- helpers
def _json(text):
    starts = [i for i in ((text or "").find("["), (text or "").find("{")) if i >= 0]
    if not starts:
        return None
    try:
        return json.loads(text[min(starts):])
    except ValueError:
        return None


def _tail(text, n=8):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def explain(text):
    """Advice for a known pymobiledevice3 failure in `text` (stderr or a log), else None."""
    lines = [l for l in (text or "").splitlines() if " INFO " not in l and " DEBUG " not in l]
    low = "\n".join(lines).lower()
    for needles, message in _HINTS:
        if any(n in low for n in needles):
            return message
    return None


def failure_text(text, what):
    return explain(text) or f"{what}:\n{_tail(text, 6)}"


def parse_version(version):
    """'17.4.1' -> (17, 4); anything unparsable -> (0, 0)."""
    parts = (str(version or "").split(".") + ["0", "0"])[:2]
    try:
        return int(parts[0] or 0), int(parts[1] or 0)
    except ValueError:
        return 0, 0


def usbmuxd_address():
    """(host, port) of usbmuxd, or a unix socket path."""
    addr = os.environ.get("PYMOBILEDEVICE3_USBMUX") or os.environ.get("USBMUXD_SOCKET_ADDRESS")
    if addr:
        if addr.upper().startswith("UNIX:"):
            return addr[5:]
        host, sep, port = addr.rpartition(":")
        if sep and port.isdigit() and "/" not in addr:
            return host or "127.0.0.1", int(port)
        return addr
    return "/var/run/usbmuxd"


def usbmuxd_reachable(timeout=1.5):
    addr = usbmuxd_address()
    if isinstance(addr, tuple):
        try:
            with socket.create_connection(addr, timeout=timeout):
                return True
        except OSError:
            return False
    return os.path.exists(addr)


# ---------------------------------------------------------------------- device queries
def list_devices(cancel=None):
    """iPhones on USB: dicts with Identifier, DeviceName, ProductVersion, ProductType, ..."""
    if not usbmuxd_reachable():
        raise UsbError(MSG_NO_USBMUXD)
    code, out, err = pmd3("usbmux", "list", "--usb", timeout=30, cancel=cancel)
    if code == 0:
        devices = _json(out)
        return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
    hint = explain(err)
    if hint is None:
        # reading the device details needs lockdown; is there a device at all?
        code2, out2, _ = pmd3("usbmux", "list", "--usb", "--simple", timeout=30, cancel=cancel)
        udids = _json(out2) if code2 == 0 else None
        if isinstance(udids, list) and any(isinstance(u, str) for u in udids):
            hint = MSG_NOT_TRUSTED
    if hint:
        raise UsbError(hint)
    raise UsbError("Could not list the USB devices:\n" + _tail(err, 6))


def developer_mode_enabled(udid, cancel=None):
    code, out, err = pmd3("amfi", "developer-mode-status", "--udid", udid, timeout=30, cancel=cancel)
    if code:
        raise UsbError(failure_text(err or out, "Could not read the Developer Mode status"))
    return out.strip().lower().startswith("true")


def find_wda_bundle(udid, cancel=None):
    """Bundle id of the installed WebDriverAgent runner, or None."""
    code, out, err = pmd3("apps", "list", "--type", "User", "--udid", udid, timeout=60, cancel=cancel)
    if code:
        raise UsbError(failure_text(err or out, "Could not list the apps on the iPhone"))
    apps = _json(out) or {}
    if isinstance(apps, list):
        apps = {a.get("CFBundleIdentifier", ""): a for a in apps if isinstance(a, dict)}
    if not isinstance(apps, dict):
        return None

    def is_wda(bundle, info):
        names = [bundle]
        if isinstance(info, dict):  # re-signing tools may rename the bundle id
            names += [str(info.get(k) or "") for k in ("CFBundleName", "CFBundleExecutable", "CFBundleDisplayName")]
        return any("webdriveragent" in n.lower() for n in names)

    candidates = [b for b, info in apps.items() if b and is_wda(b, info)]
    candidates.sort(key=lambda b: ("xctrunner" not in b.lower(), len(b)))
    return candidates[0] if candidates else None


def free_port(preferred=WDA_PORT):
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise UsbError("no free local TCP port")


def wda_ready(base_url, timeout=2.0):
    """True if WebDriverAgent answers /status at base_url (no system proxy is used)."""
    if not base_url:
        return False
    try:
        with WDA(base_url, timeout=timeout) as wda:
            value = wda.status(timeout=timeout)
    except (WDAError, ValueError):
        return False
    return bool(value) and bool(value.get("ready", True))


# ---------------------------------------------------------------------- session
class UsbSession:
    """Owns the background pymobiledevice3 processes (WDA runner + port forwarder).

    close() may be called from any thread at any time; a bring_up() in progress then stops
    within a fraction of a second with Cancelled, and nothing is left running."""

    def __init__(self):
        self.procs = []  # (name, Popen, log file)
        self.udid = None
        self.device = {}
        self.ios = (0, 0)
        self.bundle = None
        self.local_port = None
        self._closed = threading.Event()
        self._lock = threading.Lock()

    @property
    def base_url(self):
        port = self.local_port
        return f"http://127.0.0.1:{port}" if port else None

    @property
    def closed(self):
        return self._closed.is_set()

    def bring_up(self, progress=lambda msg: None, wait=90):
        cancel = self._closed
        progress("Looking for the iPhone on USB...")
        devices = list_devices(cancel=cancel)
        if not devices:
            raise UsbError(MSG_NO_DEVICE)
        if len(devices) > 1:
            raise UsbError(MSG_MULTIPLE_DEVICES)
        self.device = devices[0]
        self.udid = self.device.get("Identifier") or self.device.get("UniqueDeviceID")
        if not self.udid:
            raise UsbError("usbmuxd reported an iPhone without a UDID")
        version = str(self.device.get("ProductVersion") or "0")
        self.ios = parse_version(version)
        name = self.device.get("DeviceName") or "iPhone"
        log.info("USB device %s (%s, iOS %s)", name, self.udid, version)

        if self.ios[0] >= 16:
            progress(f"{name} (iOS {version}): checking Developer Mode...")
            if not developer_mode_enabled(self.udid, cancel=cancel):
                raise UsbError(MSG_DEV_MODE)

        progress("Mounting the Developer Disk Image (downloaded the first time)...")
        code, out, err = pmd3("mounter", "auto-mount", "--udid", self.udid, timeout=600, cancel=cancel)
        if code:
            raise UsbError(failure_text(err or out, "Could not mount the Developer Disk Image"))

        progress("Looking for WebDriverAgent...")
        self.bundle = find_wda_bundle(self.udid, cancel=cancel)
        if not self.bundle:
            raise UsbError(MSG_NO_WDA)

        progress("Starting WebDriverAgent on the iPhone...")
        self._spawn("wda_runner", xcuitest_args(self.bundle, self.udid, self.ios))
        self.local_port = free_port()
        self._spawn("wda_forward", forward_args(self.local_port, self.udid))

        progress("Waiting for WebDriverAgent to answer...")
        deadline = time.monotonic() + wait
        while True:
            url = self.base_url
            if wda_ready(url):
                return url
            self.check()
            if time.monotonic() > deadline:
                break
            if cancel.wait(1):
                raise Cancelled()
        text = self.read_log("wda_runner")
        raise UsbError(explain(text) or MSG_WDA_TIMEOUT + "\n" + _tail(text))

    def check(self):
        """Raise UsbError if the session was closed or one of its processes has exited."""
        if self._closed.is_set():
            raise Cancelled()
        for name, p, _ in list(self.procs):
            if p.poll() is not None:
                text = self.read_log(name)
                what = "WebDriverAgent stopped" if name == "wda_runner" else "The USB port forwarder stopped"
                raise UsbError(explain(text) or f"{what} (exit code {p.returncode}):\n{_tail(text)}")

    def log_path(self, name):
        return APP_DIR / f"{name}.log"

    def read_log(self, name):
        try:
            return self.log_path(name).read_text("utf-8", "replace")
        except OSError:
            return ""

    def _spawn(self, name, args):
        with self._lock:
            if self._closed.is_set():
                raise Cancelled()
            APP_DIR.mkdir(parents=True, exist_ok=True)
            logf = open(self.log_path(name), "w", encoding="utf-8", errors="replace")
            logf.write("pymobiledevice3 " + " ".join(args) + "\n")
            logf.flush()
            log.info("spawn %s: %s", name, " ".join(args))
            try:
                p = _popen(args, stdout=logf, stderr=subprocess.STDOUT)
            except (OSError, UsbError) as e:
                logf.close()
                if isinstance(e, UsbError):
                    raise
                raise UsbError(f"Could not start pymobiledevice3: {describe_error(e)}") from None
            self.procs.append((name, p, logf))

    def close(self):
        with self._lock:
            self._closed.set()
            procs, self.procs = self.procs, []
        for _, p, _ in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        for _, p, logf in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill(p)
                p.wait()
            logf.close()
        self.local_port = None


def xcuitest_args(bundle, udid, ios=(0, 0)):
    args = ["developer", "dvt", "xcuitest", bundle, "--udid", udid]
    if (17, 0) <= tuple(ios) < (17, 4):
        args.extend(("--tunnel", udid))
    elif tuple(ios) >= (17, 4):
        args.append("--userspace")
    return args


def forward_args(local_port, udid):
    return ["usbmux", "forward", str(local_port), str(WDA_PORT), "--host", "127.0.0.1", "--udid", udid]


# ---------------------------------------------------------------------- WebDriverAgent download
def _ipa_member(name):
    parts = name.split("/")
    return not (
        name.endswith("/")
        or name.startswith(("/", "__MACOSX/"))
        or ".." in parts
        or parts[-1].startswith("._")
        or parts[-1] == ".DS_Store"
    )


def download_wda_ipa(dest: Path = None, progress=print, url: str = WDA_RELEASE_URL) -> Path:
    """Fetch Appium's unsigned real-device WDA build and repackage it as an .ipa
    (Payload/<name>.app/...), ready to be signed and installed."""
    dest = Path(dest) if dest else downloads_dir() / "WebDriverAgentRunner.ipa"
    progress(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "iphone-mirror"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise UsbError(f"Download failed: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        reason = describe_error(e.reason) if isinstance(e.reason, OSError) else str(e.reason)
        raise UsbError(f"Download failed: {reason}") from None
    except OSError as e:
        raise UsbError(f"Download failed: {describe_error(e)}") from None
    progress(f"Downloaded {len(data) / 1e6:.1f} MB, building the .ipa...")
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UsbError("The download is not a zip archive") from None
    with src:
        names = [n for n in src.namelist() if _ipa_member(n)]
        roots = sorted({n[: n.find(".app/") + 5] for n in names if ".app/" in n}, key=len)
        if not roots:
            raise UsbError("Unexpected archive layout: no .app bundle inside")
        app_root = roots[0]  # outermost bundle, e.g. "WebDriverAgentRunner-Runner.app/"
        prefix = app_root[: app_root.rstrip("/").rfind("/") + 1]  # folder containing the .app
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as ipa:
                for n in names:
                    if not n.startswith(app_root):
                        continue
                    info = src.getinfo(n)
                    zi = zipfile.ZipInfo("Payload/" + n[len(prefix):], info.date_time)
                    zi.create_system = info.create_system  # keep unix modes (executables, symlinks)
                    zi.external_attr = info.external_attr
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    ipa.writestr(zi, src.read(n))
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    progress(f"Saved {dest}")
    return dest
