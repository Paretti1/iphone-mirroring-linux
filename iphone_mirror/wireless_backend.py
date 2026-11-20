"""Start the wireless decoder with tightly packed frames for the desktop viewer."""

import asyncio
import logging
import runpy

import av
import numpy as np

MAX_DISPLAY_EDGE = 1440


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


def main():
    configure_backend()
    runpy.run_module("pymobiledevice3", run_name="__main__")


if __name__ == "__main__":
    main()
