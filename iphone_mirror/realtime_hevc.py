"""Decode HEVC references in order; convert only the newest picture for display."""

import contextlib
import logging
import os
import queue
import threading
import time

import av

log = logging.getLogger(__name__)
MAX_DISPLAY_EDGE = 1440
MAX_QUEUED_PACKETS = 12


def preview_edge():
    try:
        edge = int(os.environ.get("IPHONE_MIRROR_PREVIEW_EDGE", "1080"))
    except ValueError:
        return 1080
    return edge if edge in (720, 1080, 1440) else 1080


def is_keyframe(packet):
    """Find an HEVC random access NAL in an Annex-B access unit."""
    start = 0
    while (start := packet.find(b"\x00\x00\x01", start)) >= 0:
        header = start + 3
        if header < len(packet) and (packet[header] >> 1) & 0x3F in (19, 20, 21):
            return True
        start = header
    return False


class RealtimeHevcDecoder:
    """Use separate decode and conversion workers with a single preview slot.

    Every predictive access unit is decoded while the codec can keep up. Expensive
    resizing, BGRA conversion, and delivery cannot delay HEVC reference decoding.
    If decoding itself falls behind, discard the entire pending reference chain
    and request a keyframe rather than accumulate seconds of obsolete video.
    """

    def __init__(self, vps, sps, pps, *, on_frame, on_decode_error=None):
        from pymobiledevice3.remote.core_device.hevc_rps import parse_sps, remove_emulation_prevention

        state = parse_sps(remove_emulation_prevention(sps[2:]))
        self.source_width = state.pic_width_in_luma_samples
        self.source_height = state.pic_height_in_luma_samples
        if min(self.source_width, self.source_height) <= 0 or self.source_width * self.source_height > 4096 * 4096:
            raise ValueError("Invalid HEVC display size")
        scale = min(1.0, preview_edge() / max(self.source_width, self.source_height))
        self.width = max(1, round(self.source_width * scale))
        self.height = max(1, round(self.source_height * scale))
        self._parameter_sets = b"".join(b"\x00\x00\x00\x01" + nal for nal in (vps, sps, pps))
        self._on_frame = on_frame
        self._on_decode_error = on_decode_error
        self._inq = queue.Queue(maxsize=MAX_QUEUED_PACKETS)
        self._stop = threading.Event()
        self._feed_lock = threading.Lock()
        self._preview_ready = threading.Condition()
        self._preview = None
        self._generation = 0
        self._waiting_for_keyframe = False
        self._last_recovery = 0.0
        self.decoded_frames = self.presented_frames = 0
        self.last_preview_ms = 0.0
        self.skipped_previews = self.dropped_packets = self.recoveries = 0
        self._present_thread = threading.Thread(target=self._present, name="hevc-preview", daemon=True)
        self._thread = threading.Thread(target=self._run, name="hevc-decode", daemon=True)
        self._present_thread.start()
        self._thread.start()

    def _new_codec(self):
        codec = av.CodecContext.create("hevc", "r")
        codec.thread_type = "SLICE"
        codec.thread_count = min(4, os.cpu_count() or 1)
        codec.options = {"flags": "low_delay", "max_pixels": str(4096 * 4096)}
        with contextlib.suppress(av.error.FFmpegError):
            codec.decode(av.Packet(self._parameter_sets))
        return codec

    def _request_recovery(self):
        now = time.monotonic()
        if now - self._last_recovery >= 0.8:
            self._last_recovery = now
            if self._on_decode_error is not None:
                self._on_decode_error()

    def feed(self, annexb):
        if self._stop.is_set():
            return
        request_recovery = False
        with self._feed_lock:
            if self._inq.full():
                # Individual P-frames cannot be dropped safely. Abandon this
                # whole generation, then resume only at a random access point.
                self._generation += 1
                self._waiting_for_keyframe = True
                self.recoveries += 1
                while True:
                    try:
                        self._inq.get_nowait()
                        self.dropped_packets += 1
                    except queue.Empty:
                        break
            if self._waiting_for_keyframe:
                if is_keyframe(annexb):
                    self._waiting_for_keyframe = False
                else:
                    self.dropped_packets += 1
                    request_recovery = True
            if not self._waiting_for_keyframe:
                self._inq.put_nowait((self._generation, annexb, time.monotonic()))
        if request_recovery:
            self._request_recovery()

    @property
    def queue_age_ms(self):
        with self._inq.mutex:
            item = self._inq.queue[0] if self._inq.queue else None
            return round((time.monotonic() - item[2]) * 1000) if item else 0

    def _run(self):
        codec = None
        active_generation = -1
        while not self._stop.is_set():
            try:
                item = self._inq.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                break
            generation, packet, arrived = item
            if generation != self._generation:
                continue
            try:
                if generation != active_generation:
                    codec = self._new_codec()
                    active_generation = generation
                frames = codec.decode(av.Packet(packet))
            except Exception:
                log.debug("HEVC decode failed", exc_info=True)
                self._request_recovery()
                continue
            for frame in frames:
                self.decoded_frames += 1
                with self._preview_ready:
                    if generation != self._generation or self._stop.is_set():
                        continue
                    if self._preview is not None:
                        self.skipped_previews += 1
                    self._preview = (generation, frame, arrived)
                    self._preview_ready.notify()

    def _convert(self, frame):
        # Resize in libswscale directly from YUV to the preview-sized BGRA
        # buffer. Avoid a full-size BGRA conversion followed by a second resize.
        return frame.reformat(width=self.width, height=self.height, format="bgra").to_ndarray().tobytes()

    def _present(self):
        while not self._stop.is_set():
            with self._preview_ready:
                self._preview_ready.wait_for(lambda: self._preview is not None or self._stop.is_set())
                if self._stop.is_set():
                    break
                generation, frame, arrived = self._preview
                self._preview = None
            try:
                pixels = self._convert(frame)
                if not self._stop.is_set() and generation == self._generation:
                    self._on_frame(pixels)
                    self.last_preview_ms = (time.monotonic() - arrived) * 1000
                    self.presented_frames += 1
            except Exception:
                log.exception("HEVC preview conversion failed")

    def close(self):
        self._stop.set()
        with self._preview_ready:
            self._preview = None
            self._preview_ready.notify_all()
        # The decoder wakes at least every 50 ms; no blocking queue insertion
        # or cross-thread codec destruction is needed to stop it.
        self._thread.join(timeout=2)
        self._present_thread.join(timeout=2)
