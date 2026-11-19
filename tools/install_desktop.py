"""Register the desktop launcher and icon in the current user's XDG directories."""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from iphone_mirror.config import APP_ID, APP_NAME, xdg_home
from iphone_mirror.ui import app_icon


def desktop_exec(path):
    # First quote the Exec argument, then escape it for the Desktop Entry format.
    text = str(path)
    for character in ('\\', '"', '`', '$'):
        text = text.replace(character, '\\' + character)
    return '"' + text.replace('\\', '\\\\').replace('%', '%%') + '"'


def main(data_home):
    commands = Path(sys.executable).parent
    launcher = commands / "iphone-mirroring-linux"
    icon_dir = data_home / "icons/hicolor/128x128/apps"
    menu_dir = data_home / "applications"
    icon_dir.mkdir(parents=True, exist_ok=True)
    menu_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    icon = app_icon(128).pixmap(QSize(128, 128))
    if not icon.save(str(icon_dir / f"{APP_ID}.png")):
        raise OSError("could not save desktop icon")
    (menu_dir / f"{APP_ID}.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Mirror your iPhone screen and audio over Wi-Fi\n"
        f"Exec={desktop_exec(launcher)}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;\n"
        f"StartupWMClass={APP_ID}\n",
        encoding="utf-8",
    )
    bin_dir = xdg_home("XDG_BIN_HOME", Path.home() / ".local/bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for command in (launcher, commands / "iphone-mirroring-control"):
        link = bin_dir / command.name
        try:
            link.symlink_to(command)
        except FileExistsError:
            if not link.is_symlink() or link.resolve() != command.resolve():
                print(f"Existing command left in place. Use {command}", file=sys.stderr)
    if str(bin_dir) not in os.get_exec_path():
        print(f"Terminal commands are installed in {bin_dir}. Add it to PATH or use their full paths.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
