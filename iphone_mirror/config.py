"""Persistent settings and identity (name, MAC, Ed25519 key) of the receiver."""
import json
import logging
import os
import secrets
import socket
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

APP_ID = "io.github.Paretti1.IPhoneMirroringLinux"
APP_NAME = "iPhone Mirroring Linux"


def xdg_home(variable, fallback):
    value = Path(os.environ.get(variable) or fallback)
    return value if value.is_absolute() else Path(fallback)


def downloads_dir():
    try:
        value = subprocess.check_output(
            ["xdg-user-dir", "DOWNLOAD"], text=True, timeout=2
        ).strip()
        if Path(value).is_absolute():
            return Path(value)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Downloads"


CONFIG_DIR = xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / "iphone-mirroring-linux"
APP_DIR = xdg_home("XDG_STATE_HOME", Path.home() / ".local/state") / "iphone-mirroring-linux"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = APP_DIR / "iphone_mirror.log"

DEFAULTS = {
    # name shown on the iPhone in Control Center -> Screen Mirroring
    "name": "",
    # resolution requested from the iPhone; the height controls the stream size
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "audio": True,
    "always_on_top": True,
    # window geometry (logical pixels); 0 = automatic
    "x": None,
    "y": None,
    "phone_height": 0,
    # identity, generated on first start
    "mac": "",
    "ed25519_seed": "",
}


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE, default_name: str = ""):
        self.path = path
        self.data = dict(DEFAULTS, name=default_name)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("expected a JSON object")
                self.data.update(loaded)
            except Exception as e:  # corrupt file: keep defaults, rewrite
                log.warning("settings.json unreadable (%s), using defaults", e)
        changed = False
        if not self.data.get("name"):
            self.data["name"] = socket.gethostname() or APP_NAME
            changed = True
        if not self.data.get("mac"):
            mac = bytearray(secrets.token_bytes(6))
            mac[0] = (mac[0] | 0x02) & 0xFE  # locally administered, unicast
            self.data["mac"] = mac.hex()
            changed = True
        if not self.data.get("ed25519_seed"):
            self.data["ed25519_seed"] = secrets.token_hex(32)
            changed = True
        if changed:
            self.save()

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

    def save(self):
        tmp = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as file:
                tmp = Path(file.name)
                json.dump(self.data, file, indent=2, ensure_ascii=False)
                file.write("\n")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save settings: %s", e)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.data["mac"])

    @property
    def device_id(self) -> str:  # aa:bb:cc:dd:ee:ff
        return ":".join(f"{b:02x}" for b in self.mac_bytes)

    @property
    def raop_prefix(self) -> str:  # AABBCCDDEEFF
        return self.mac_bytes.hex().upper()
