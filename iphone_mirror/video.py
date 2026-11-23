"""Screen-mirroring video: TCP stream parsing, AES-CTR decryption, H.264 decoding."""
import asyncio
import hashlib
import logging
import math
import queue
import struct
import threading
import time

import av
import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import net

log = logging.getLogger(__name__)

START = b"\x00\x00\x00\x01"
MAX_DECODE_PIXELS = 4096 * 4096
MAX_VIDEO_EDGE = 4096
MAX_VIDEO_NALS = 1024
MEDIA_SETUP_TIMEOUT = 15


class VideoQueue(queue.Queue):
    """Bound compressed frames by bytes as well as packet count."""

    def __init__(self, max_bytes=32 * 1024 * 1024):
        super().__init__(maxsize=32)
        self.max_bytes = max_bytes
        self.buffered_bytes = 0

    def _put(self, item):
        size = len(item) if item is not None else 0
        if self.buffered_bytes + size > self.max_bytes:
            raise queue.Full
        super()._put(item)
        self.buffered_bytes += size

    def _get(self):
        item = super()._get()
        self.buffered_bytes -= len(item) if item is not None else 0
        return item


def mirror_cipher(aeskey: bytes, stream_connection_id: int):
    """AES-128-CTR decryptor for the mirror stream (key/iv derived like the receiver)."""
    skey = f"AirPlayStreamKey{stream_connection_id}".encode()
    siv = f"AirPlayStreamIV{stream_connection_id}".encode()
    key = hashlib.sha512(skey + aeskey).digest()[:16]
    iv = hashlib.sha512(siv + aeskey).digest()[:16]
    return Cipher(algorithms.AES(key), modes.CTR(iv))


def avcc_to_annexb(data: bytes):
    """4-byte big-endian length prefixed NALs -> start-code NALs. None if malformed."""
    out = bytearray()
    pos, n = 0, len(data)
    count = 0
    while pos < n:
        count += 1
        if pos + 4 > n or count > MAX_VIDEO_NALS:
            return None
        size = int.from_bytes(data[pos:pos + 4], "big")
        if size <= 0 or pos + 4 + size > n or data[pos + 4] & 0x80:
            return None
        out += START
        out += data[pos + 4:pos + 4 + size]
        pos += 4 + size
    return bytes(out)


class VideoDecoder(threading.Thread):
    """Decodes Annex-B access units; hands the newest frame (BGRA ndarray) to on_frame."""

    def __init__(self, on_frame):
        super().__init__(name="video-decoder", daemon=True)
        self.on_frame = on_frame
        self.q = VideoQueue()
        self.target = None  # (w, h) in device pixels, set by the UI
        self.last_frame = None  # full-resolution av.VideoFrame (for screenshots)
        self._codec = None
        self._stop = False

    def submit(self, data: bytes):
        try:
            self.q.put_nowait(data)
        except queue.Full:
            log.debug("decoder queue full, dropping frame")

    def reset(self):
        self.last_frame = None
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.submit(None)

    def stop(self):
        self._stop = True
        self.submit(b"")

    def _new_codec(self):
        c = av.CodecContext.create("h264", "r")
        try:
            c.thread_type = "SLICE"  # no frame-threading delay
        except Exception:
            pass
        c.options = {"flags": "low_delay", "max_pixels": str(MAX_DECODE_PIXELS)}
        return c

    def run(self):
        while not self._stop:
            data = self.q.get()
            if self._stop:
                break
            if data is None:
                self._codec = None
                continue
            if self._codec is None:
                self._codec = self._new_codec()
            try:
                frames = self._codec.decode(av.Packet(data))
            except av.error.FFmpegError as e:
                log.debug("decode error: %s", e)
                continue
            if not frames or not self.q.empty():
                continue  # behind: skip conversion, keep decoding references
            frame = frames[-1]
            try:
                self._deliver(frame)
            except Exception:
                log.exception("frame conversion failed")

    def _deliver(self, frame):
        if not (0 < frame.width <= MAX_VIDEO_EDGE and 0 < frame.height <= MAX_VIDEO_EDGE):
            return
        self.last_frame = frame
        tw, th = self.target or (frame.width, frame.height)
        tw, th = max(16, int(tw)), max(16, int(th))
        # keep the source aspect if the UI has not adapted yet
        if abs(tw / th - frame.width / frame.height) > 0.02:
            th = max(16, round(tw * frame.height / frame.width))
        scale = min(1, MAX_VIDEO_EDGE / max(tw, th))
        img = frame.reformat(width=max(1, round(tw * scale)), height=max(1, round(th * scale)),
                             format="bgra", interpolation="AREA").to_ndarray()
        img = np.ascontiguousarray(img)  # ffmpeg rows are padded; QImage wants a packed buffer
        self.on_frame(img, frame.width, frame.height)


MAX_VIDEO_PACKET = 16 * 1024 * 1024


class MirrorStream:
    """Accepts the iPhone's mirror TCP connection and feeds the decoder."""

    def __init__(self, aeskey: bytes, stream_connection_id: int, decoder: VideoDecoder, events, remote_ip):
        self.decryptor = mirror_cipher(aeskey, stream_connection_id).decryptor()
        self.decoder = decoder
        self.events = events  # callable(event: str, **info)
        self.server = None
        self.remote_ip = remote_ip
        self.port = 0
        self._sps_pps = None
        self._checked = False
        self._bad = 0
        self._task = None
        self.frames = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, host=net.BIND_HOST, port=net.PORT + net.VIDEO_OFFSET)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("mirror data port %d", self.port)
        return self.port

    async def stop(self):
        server, self.server = self.server, None
        if server:
            server.close()
        if self._task:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if server:
            await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] != self.remote_ip or self._task is not None:
            writer.close()
            return
        self._task = asyncio.current_task()
        log.info("mirror stream connected from %s", peer)
        sock = writer.get_extra_info("socket")
        try:
            import socket as _s
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception:
            pass
        self.decoder.reset()
        try:
            # Leave established, idle phones connected. Bound the first byte
            # of a new media stream and the remainder of each started packet.
            async with asyncio.timeout(MEDIA_SETUP_TIMEOUT):
                first = await reader.readexactly(1)
            while True:
                async with asyncio.timeout(MEDIA_SETUP_TIMEOUT):
                    hdr = first + await reader.readexactly(127)
                    size = struct.unpack_from("<I", hdr, 0)[0]
                    if size > MAX_VIDEO_PACKET:
                        raise ValueError("video packet exceeds the size limit")
                    payload = await reader.readexactly(size) if size else b""
                self._packet(hdr, payload)
                first = await reader.readexactly(1)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ValueError):
            log.info("mirror stream closed")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("mirror stream error")
        finally:
            writer.close()
            if self._task is asyncio.current_task():
                self._task = None
                self.events("mirror_stopped")

    def _packet(self, hdr: bytes, payload: bytes):
        ptype = hdr[4]
        if ptype == 0:  # encrypted video access unit
            if not payload:
                return
            dec = self.decryptor.update(payload)
            nal = avcc_to_annexb(dec)
            if nal is None:
                self._bad += 1
                if not self._checked:
                    self._checked = True
                    log.error("first video packet failed to decrypt into valid H.264 (key mismatch?)")
                    self.events("decrypt_error")
                return
            if not self._checked:
                self._checked = True
                log.info("video decryption OK")
            if self._sps_pps:
                nal = self._sps_pps + nal
                self._sps_pps = None
            self.frames += 1
            self.decoder.submit(nal)
        elif ptype == 1:  # unencrypted codec config (SPS + PPS)
            if payload[4:8] == b"hvc1":
                log.error("client sent H.265, which is not supported")
                return
            if len(payload) < 8:
                raise ValueError("incomplete AirPlay H.264 configuration")
            sps_size = struct.unpack_from(">H", payload, 6)[0]
            if not sps_size or len(payload) < sps_size + 11:
                raise ValueError("invalid AirPlay H.264 SPS")
            sps = payload[8:8 + sps_size]
            pps_size = struct.unpack_from(">H", payload, sps_size + 9)[0]
            if not pps_size or len(payload) < sps_size + 11 + pps_size:
                raise ValueError("invalid AirPlay H.264 PPS")
            pps = payload[sps_size + 11:sps_size + 11 + pps_size]
            self._sps_pps = START + sps + START + pps
            w, h = struct.unpack_from("<ff", hdr, 56)
            if hdr[6] in (0x56, 0x5E):
                self.events("paused")
            elif all(math.isfinite(edge) and 1 <= edge <= MAX_VIDEO_EDGE for edge in (w, h)):
                self.events("format", width=int(w), height=int(h))
        # 2 = heartbeat (old protocol), 5 = streaming report: nothing to do
