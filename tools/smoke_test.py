"""Stream simulated AirPlay audio and video twice to the local Qt receiver."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def choose_port():
    for port in range(17100, 17200, 5):
        with ExitStack() as sockets:
            try:
                for offset in range(5):
                    kind = socket.SOCK_STREAM if offset < 2 else socket.SOCK_DGRAM
                    endpoint = sockets.enter_context(socket.socket(type=kind))
                    endpoint.bind(("127.0.0.1", port + offset))
                return port
            except OSError:
                continue
    raise RuntimeError("no free loopback port block available for the smoke test")


def run_smoke_test(tmp):
    port = choose_port()
    log_path = tmp / "receiver.log"
    grabs = tmp / "frames"
    environment = dict(
        os.environ,
        QT_QPA_PLATFORM="offscreen",
        XDG_CONFIG_HOME=str(tmp / "config"),
        XDG_STATE_HOME=str(tmp / "state"),
    )
    with log_path.open("w") as log:
        args = [PYTHON, "-m", "iphone_mirror", "--local", "--port", str(port), "--grab", str(grabs)]
        receiver = subprocess.Popen(args, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while f"RTSP server listening on port {port}" not in log_path.read_text():
                if receiver.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Qt receiver did not start")
                time.sleep(0.1)
            for _ in range(2):
                subprocess.run(
                    [PYTHON, str(ROOT / "tools/fake_iphone.py"), "127.0.0.1", str(port), "3"],
                    cwd=ROOT, check=True, timeout=12,
                )
        finally:
            if receiver.poll() is None:
                receiver.terminate()
            try:
                receiver.wait(timeout=8)
            except subprocess.TimeoutExpired:
                receiver.kill()
                receiver.wait()
                raise RuntimeError("Qt receiver did not shut down") from None
    output = log_path.read_text()
    if receiver.returncode or "shutdown timed out" in output:
        raise RuntimeError("Qt receiver shut down with an error")
    for message in ("video decryption OK", "audio decoding OK"):
        if output.count(message) != 2:
            raise RuntimeError(f"expected {message!r} once per connection")
    if not list(grabs.glob("window_*.png")):
        raise RuntimeError("Qt produced no window captures")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="iphone-mirror-smoke-") as directory:
        tmp = Path(directory)
        try:
            run_smoke_test(tmp)
        except Exception:
            log = tmp / "receiver.log"
            if log.exists():
                print(log.read_text(), file=sys.stderr)
            raise
    print("AirPlay smoke test passed: video, audio, Qt capture, reconnect, and shutdown.")
