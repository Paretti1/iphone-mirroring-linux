"""Local viewer commands for iOS's user-approved developer pairing."""

import argparse
import logging
import os
import secrets
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

from .usb import MSG_NO_PMD3

PAIR_PORT = 7005
WEB_PORT = 8765
VNC_PORT = 5901
MIN_VERSION = (11, 61)
LOOPBACK = "127.0.0.1"

log = logging.getLogger(__name__)


def port_number(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("expected a port from 1024 to 65535")
    return port


def dependency_error():
    try:
        installed = tuple(int(v) for v in version("pymobiledevice3").split(".")[:2])
    except PackageNotFoundError:
        return MSG_NO_PMD3
    if installed < MIN_VERSION:
        return "Wireless control requires pymobiledevice3 11.61 or later. Rerun ./install.sh --control."
    return None


def remote_args(device_id):
    args = ["--remotepairing"]
    if device_id:
        args.extend(("--udid", device_id))
    return args


def run_tool(args):
    error = dependency_error()
    if error:
        print(error, file=sys.stderr)
        return 1
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        return subprocess.call([sys.executable, "-m", "pymobiledevice3", *args], env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        log.error("could not start pymobiledevice3: %s", e)
        return 1


def pair(args):
    if args.usb:
        return run_tool(["lockdown", "remotepairing", "--pair"])
    options = ["remote", "pair-host", "--port", str(args.port), "--timeout", "180"]
    if args.name:
        options.extend(("--name", args.name))
    return run_tool(options)


def viewer(args):
    port = args.port or (VNC_PORT if args.vnc else WEB_PORT)
    remote = remote_args(args.device_id)
    result = run_tool(["mounter", "auto-mount", *remote])
    if result:
        return result
    # Upstream VNC Authentication uses the first eight characters.
    password = secrets.token_hex(4 if args.vnc else 12)
    options = ["developer", "core-device", "display"]
    if args.vnc:
        options += ["serve-vnc", "--port", str(port), "--decoder", "av"]
        print(f"Open vnc://{LOOPBACK}:{port} in a VNC viewer. Use password {password}.", flush=True)
    else:
        options += ["serve-web", "--http-port", str(port)]
        if not args.audio:
            options.append("--no-audio")
        print(f"Open http://{LOOPBACK}:{port}/ after the server starts.", flush=True)
        print(f"Use username iphone and password {password}.", flush=True)
    options += ["--bind", LOOPBACK, "--password", password, *remote]
    return run_tool(options)


def register(subparsers):
    pair_parser = subparsers.add_parser("wireless-pair", help="pair over Wi-Fi using the iPhone's PIN prompt (iOS 27+)")
    pair_parser.add_argument("--usb", action="store_true", help="save wireless pairing over a trusted USB connection once")
    pair_parser.add_argument("--name", help="name shown in Paired Macs (default: computer hostname)")
    pair_parser.add_argument("--port", type=port_number, default=PAIR_PORT, help=f"pairing port (default: {PAIR_PORT})")
    pair_parser.set_defaults(func=pair)

    parser = subparsers.add_parser("wireless", help="start a local interactive viewer for a paired iPhone (iOS 27+)")
    parser.add_argument("--device-id", metavar="UDID", help="device identifier printed by wireless-pair")
    parser.add_argument("--port", type=port_number, help=f"local viewer port (defaults: web {WEB_PORT}, VNC {VNC_PORT})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--vnc", action="store_true", help="use a local VNC viewer with native HEVC decoding")
    mode.add_argument("--audio", action="store_true", help="enable sound by default in the web viewer")
    parser.set_defaults(func=viewer)
