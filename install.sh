#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' 'Usage: ./install.sh [--control]' \
        'Install the desktop app for the current user.' \
        'Include optional Wi-Fi and USB control with --control (or --usb).' \
        'Set PYTHON=/path/to/python3.12 to choose another Python.'
}

extras=''
case "${1:-}" in
    '') ;;
    --usb|--control) extras='[control]' ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    usage >&2
    exit 2
fi

if (( EUID == 0 )); then
    printf '%s\n' 'Run this installer as your regular desktop user.' >&2
    exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
[[ $data_home = /* ]] || data_home="$HOME/.local/share"
app_home="$data_home/iphone-mirroring-linux"
python=${PYTHON:-python3}

if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    printf '%s\n' 'Python 3.11 or later is required.' >&2
    exit 1
fi
mkdir -p -- "$app_home"
if [[ ! -x "$app_home/venv/bin/python" ]]; then
    "$python" -m venv "$app_home/venv"
fi
venv_python="$app_home/venv/bin/python"

cd -- "$source_dir"
"$venv_python" -m pip install --upgrade --upgrade-strategy eager ".$extras"
QT_QPA_PLATFORM=offscreen "$venv_python" tools/install_desktop.py "$data_home"

printf '\nInstalled iPhone Mirroring Linux. Open it from your application menu.\n'
