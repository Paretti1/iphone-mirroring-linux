"""Start the wireless decoder with tightly packed frames for the desktop viewer."""

import asyncio
import contextlib
import logging
import os
import runpy

import av
import numpy as np

MAX_DISPLAY_EDGE = 1440
log = logging.getLogger(__name__)


class DiagnosticTransport:
    """Count media ingress without retaining any device packets."""

    def __init__(self, transport):
        self.transport = transport
        self.packets = self.bytes = 0

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def recv(self):
        packet = await self.transport.recv()
        self.packets += 1
        self.bytes += len(packet)
        return packet


def diagnostic_backend_snapshot(viewer, transport):
    decoder = viewer._transcoder
    queue = getattr(decoder, "_inq", None)
    worker = getattr(decoder, "_thread", None)
    return (
        f"packets={transport.packets} bytes={transport.bytes} "
        f"fed={getattr(decoder, '_diagnostic_fed', 0)} "
        f"decoded={getattr(decoder, '_diagnostic_decoded', 0)} "
        f"decode_errors={getattr(decoder, '_diagnostic_errors', 0)} "
        f"published={viewer._frames_emitted} "
        f"queued={queue.qsize() if queue is not None else -1} "
        f"decoder_alive={worker.is_alive() if worker is not None else False} "
        f"refresh_pending={viewer._refresh_pending}"
    )


def diagnostic_transcoder(create):
    def wrapped(viewer, *args):
        decoder = create(viewer, *args)
        feed, on_frame, on_error = decoder.feed, decoder._on_frame, decoder._on_decode_error
        decoder._diagnostic_fed = decoder._diagnostic_decoded = decoder._diagnostic_errors = 0

        def counted_feed(packet):
            decoder._diagnostic_fed += 1
            return feed(packet)

        def counted_frame(frame):
            decoder._diagnostic_decoded += 1
            return on_frame(frame)

        def counted_error():
            decoder._diagnostic_errors += 1
            if on_error is not None:
                return on_error()

        decoder.feed = counted_feed
        decoder._on_frame = counted_frame
        decoder._on_decode_error = counted_error
        return decoder

    return wrapped


def diagnostic_receiver(receive):
    async def wrapped(viewer, transport):
        counted = DiagnosticTransport(transport)

        async def report():
            while True:
                log.info("WIRELESS_DIAG backend %s", diagnostic_backend_snapshot(viewer, counted))
                await asyncio.sleep(5)

        monitor = asyncio.create_task(report())
        try:
            await receive(viewer, counted)
        except Exception as exc:
            log.exception("WIRELESS_DIAG ingress_failed type=%s", type(exc).__name__)
            raise
        finally:
            log.info("WIRELESS_DIAG ingress_stopped %s", diagnostic_backend_snapshot(viewer, counted))
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

    return wrapped


def emit_packed_frame(transcoder, frame):
    """Remove FFmpeg row padding before handing BGRA pixels to the VNC server."""
    image = frame.reformat(width=transcoder.width, height=transcoder.height, format="bgra")
    pixels = image.to_ndarray()
    pixels[:, :, 3] = 255
    transcoder._on_frame(pixels.tobytes())


def present_desktop_frame(viewer, bgra, width, height):
    """Crop and orient the decoded picture, then limit the local transport size."""
    from pymobiledevice3.remote.core_device.vnc_server import content_size

    crop_width, crop_height = content_size(width, height, viewer._display_size)
    pixels = np.frombuffer(bgra, dtype=np.uint8).reshape(height, width, 4)
    pixels = pixels[:crop_height, :crop_width]
    rotation = viewer._rotation
    if rotation:
        pixels = np.rot90(pixels, -(rotation // 90))
    height, width = pixels.shape[:2]
    scale = min(1.0, MAX_DISPLAY_EDGE / max(width, height))
    if scale < 1:
        frame = av.VideoFrame.from_ndarray(pixels, format="bgra")
        frame = frame.reformat(width=max(1, round(width * scale)), height=max(1, round(height * scale)))
        pixels = frame.to_ndarray()
        height, width = pixels.shape[:2]
    return pixels.tobytes(), width, height, rotation


def request_software_keyframe(viewer, now, *, reason):
    """Keep libav decoding while requesting recovery after an actual decode error."""
    # VideoToolbox refresh heuristics gate all input until a requested IDR arrives.
    # Libav maintains its own reference state and must continue receiving access units.
    if reason not in ("decode-error", "re-arm"):
        return
    if now - viewer._last_refresh_t < 0.8:
        return
    viewer._last_refresh_t = now
    logging.getLogger(__name__).info("Requesting an HEVC recovery frame (%s)", reason)
    task = asyncio.create_task(viewer._send_rtcp_pli())
    viewer._pli_tasks.add(task)
    task.add_done_callback(viewer._pli_tasks.discard)


def configure_backend():
    """Apply Linux compatibility fixes to this viewer process only."""
    from pymobiledevice3.remote.core_device.hevc_av import HevcToBgraTranscoder
    from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

    HevcToBgraTranscoder._emit_frame = emit_packed_frame
    VncStreamServer._present = present_desktop_frame
    VncStreamServer._fire_decoder_refresh = request_software_keyframe
    if os.environ.get("IPHONE_MIRROR_DIAGNOSTICS") == "1":
        VncStreamServer._new_transcoder = diagnostic_transcoder(VncStreamServer._new_transcoder)
        VncStreamServer._udp_recv_and_pipe = diagnostic_receiver(VncStreamServer._udp_recv_and_pipe)


def main():
    configure_backend()
    runpy.run_module("pymobiledevice3", run_name="__main__")


if __name__ == "__main__":
    main()
