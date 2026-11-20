"""Start the wireless decoder with tightly packed frames for the desktop viewer."""

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


def main():
    from pymobiledevice3.remote.core_device.hevc_av import HevcToBgraTranscoder

    from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

    HevcToBgraTranscoder._emit_frame = emit_packed_frame
    VncStreamServer._present = present_desktop_frame
    runpy.run_module("pymobiledevice3", run_name="__main__")


if __name__ == "__main__":
    main()
