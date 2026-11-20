"""Keep drag input current without dropping button or keyboard transitions."""

import asyncio
import contextlib
import logging
import time

log = logging.getLogger(__name__)


class LatestMotion:
    """One in-flight move and one replaceable pending move, with ordered barriers."""

    def __init__(self, send):
        self.send = send
        self.pending = None
        self.task = None
        self.coalesced = 0
        self.sent = 0
        self.last_send_ms = 0.0

    def submit(self, payload):
        if self.task is not None and self.task.done():
            self.task.result()  # Surface a failed send rather than silently restarting it.
        if self.pending is not None:
            self.coalesced += 1
        self.pending = payload
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())

    async def _drain(self):
        while self.pending is not None:
            payload, self.pending = self.pending, None
            started = time.monotonic()
            await self.send(*payload)
            self.last_send_ms = (time.monotonic() - started) * 1000
            self.sent += 1

    async def flush(self):
        if self.task is not None:
            await self.task

    async def close(self):
        self.pending = None
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def install_low_latency_input(server_class):
    if getattr(server_class, "_linux_input_installed", False):
        return
    pointer = server_class._handle_pointer
    key = server_class._handle_key
    receive = server_class._client_recv_loop
    start_video = server_class._start_video

    def motion(viewer, client):
        queued = getattr(client, "_linux_motion", None)
        if queued is None:
            async def send(mask, x, y):
                client._linux_last_pointer = (mask, x, y)
                await pointer(viewer, client, mask, x, y)
            queued = client._linux_motion = LatestMotion(send)
        return queued

    async def latest_pointer(viewer, client, mask, x, y):
        queued = motion(viewer, client)
        if mask & 1 and client.pressed and mask == client.last_button_mask:
            queued.submit((mask, x, y))
        else:
            # Never move a press, release, or hardware-button transition across
            # an earlier drag. At most two moves can be ahead of this barrier.
            await queued.flush()
            client._linux_last_pointer = (mask, x, y)
            await pointer(viewer, client, mask, x, y)

    async def ordered_key(viewer, client, down, symbol):
        await motion(viewer, client).flush()
        await key(viewer, client, down, symbol)

    async def receive_with_motion(viewer, client):
        try:
            await receive(viewer, client)
        finally:
            queued = getattr(client, "_linux_motion", None)
            if queued is not None:
                await queued.close()
            last = getattr(client, "_linux_last_pointer", None)
            if last is not None and (client.pressed or last[0] & 1):
                # Cancellation may happen after a contact was sent but before
                # upstream updates client.pressed. Release it before closing.
                client.pressed = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(pointer(viewer, client, 0, last[1], last[2]), timeout=0.5)

    async def video_with_ready_input(viewer):
        await start_video(viewer)
        try:
            # Resolve the touchscreen and create the keyboard before the VNC
            # listener says it is ready, rather than charging the first click.
            await asyncio.wait_for(viewer._ensure_keyboard(), timeout=2)
        except Exception:
            log.info("Input preparation deferred; the first input will retry setup")

    server_class._handle_pointer = latest_pointer
    server_class._handle_key = ordered_key
    server_class._client_recv_loop = receive_with_motion
    server_class._start_video = video_with_ready_input
    server_class._linux_input_installed = True
