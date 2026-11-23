"""AirPlay screen-mirroring receiver: RTSP control connection and session setup."""
import asyncio
import hashlib
import logging
import math
import plistlib
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import discovery, net
from .audio import AudioStream, Player
from .fairplay import FairPlay
from .pairing import PairingSession, ServerIdentity
from .video import MirrorStream, VideoDecoder

log = logging.getLogger(__name__)

SERVER = f"AirTunes/{discovery.SRCVERS}"
NTP_EPOCH = 2208988800
MAX_RTSP_BODY = 1024 * 1024
MAX_RTSP_HEADER = 16 * 1024
MAX_CONNECTIONS = 8
HANDSHAKE_TIMEOUT = 10


def _bplist(obj) -> bytes:
    return plistlib.dumps(obj, fmt=plistlib.FMT_BINARY, sort_keys=False)


class Request:
    def __init__(self, method, url, protocol, headers, body):
        self.method, self.url, self.protocol = method, url, protocol
        self.headers = headers  # lower-case keys
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class Response:
    def __init__(self, status=200, reason="OK"):
        self.status, self.reason = status, reason
        self.headers = []
        self.body = b""
        self.close = False

    def set(self, body: bytes, content_type: str):
        self.body = body
        self.headers.append(("Content-Type", content_type))


class _TimingProto(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        pass  # replies are not needed: we render as soon as frames arrive


class TimingSender:
    """Sends NTP timing requests to the client like an Apple TV does."""

    def __init__(self):
        self.transport = None
        self.task = None
        self.port = 0

    async def start(self, remote_ip, remote_port):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(_TimingProto, local_addr=(net.BIND_HOST, net.PORT + net.TIMING_OFFSET))
        self.port = self.transport.get_extra_info("sockname")[1]
        if remote_port:
            self.task = asyncio.create_task(self._loop((remote_ip, remote_port)))
        return self.port

    async def _loop(self, addr):
        try:
            while True:
                now = time.time() + NTP_EPOCH
                sec = int(now)
                frac = int((now - sec) * (1 << 32)) & 0xFFFFFFFF
                pkt = bytes([0x80, 0xD2, 0x00, 0x07]) + bytes(20) + struct.pack(">II", sec, frac)
                self.transport.sendto(pkt, addr)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    def stop(self):
        if self.task:
            self.task.cancel()
        if self.transport:
            self.transport.close()


class Connection:
    def __init__(self, receiver, reader, writer):
        self.rx = receiver
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.remote_ip = peer[0]
        self.pairing = PairingSession(receiver.identity)
        self.fairplay = FairPlay()
        self.aeskey = None
        self.aesiv = None
        self.timing = None
        self.mirror = None
        self.audio = None
        self.client_name = None

    # ---------------------------------------------------------------- I/O
    async def serve(self):
        log.info("connection from %s", self.remote_ip)
        try:
            while True:
                timeout = None if self.rx.session_owner is self else HANDSHAKE_TIMEOUT
                req = await asyncio.wait_for(self._read_request(), timeout)
                if req is None:
                    break
                resp = await self._dispatch(req)
                await self._write(req, resp)
                if resp.close:
                    break
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError, TimeoutError):
            pass
        except Exception:
            log.exception("connection error")
        finally:
            try:
                await self.cleanup()
            finally:
                self.writer.close()
                log.info("connection from %s closed", self.remote_ip)

    async def _read_request(self):
        try:
            head = await self.reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None
        lines = head.decode("utf-8", "replace").split("\r\n")
        parts = lines[0].split(" ")
        if len(head) > MAX_RTSP_HEADER or len(parts) != 3:
            raise ValueError("invalid RTSP header")
        method, url, protocol = parts[0], parts[1], parts[2]
        if (protocol not in ("RTSP/1.0", "HTTP/1.0", "HTTP/1.1") or not method
                or len(method) > 32 or not all("A" <= c <= "Z" or c == "_" for c in method)):
            raise ValueError("invalid RTSP request line")
        if len(url) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError("invalid RTSP URL")
        headers = {}
        for line in lines[1:]:
            if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in line):
                raise ValueError("invalid RTSP header character")
            if not line:
                continue
            if ":" not in line:
                raise ValueError("invalid RTSP header")
            k, v = line.split(":", 1)
            if not k or any(not (c.isascii() and (c.isalnum() or c == "-")) for c in k):
                raise ValueError("invalid RTSP header name")
            if k.lower() in headers:
                raise ValueError("duplicate RTSP header")
            headers[k.lower()] = v.strip()
        if "transfer-encoding" in headers:
            raise ValueError("RTSP transfer encoding is not supported")
        n = int(headers.get("content-length", "0") or 0)
        if not 0 <= n <= MAX_RTSP_BODY:
            raise ValueError("invalid RTSP request body length")
        body = await asyncio.wait_for(self.reader.readexactly(n), HANDSHAKE_TIMEOUT) if n else b""
        return Request(method, url, protocol, headers, body)

    async def _write(self, req: Request, resp: Response):
        out = [f"{req.protocol} {resp.status} {resp.reason}"]
        cseq = req.header("CSeq")
        if cseq is not None:
            out.append(f"CSeq: {cseq}")
        out.append(f"Server: {SERVER}")
        for k, v in resp.headers:
            out.append(f"{k}: {v}")
        out.append(f"Content-Length: {len(resp.body)}")
        self.writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + resp.body)
        await self.writer.drain()

    async def _dispatch(self, req: Request) -> Response:
        path = req.url.split("?")[0]
        if req.method == "POST" and path == "/feedback":
            log.debug("%s %s", req.method, req.url)
        else:
            log.info("%s %s (%d bytes)", req.method, req.url, len(req.body))
        resp = Response()
        public = req.method == "OPTIONS" or (req.method == "GET" and path == "/info") or (
            req.method == "POST" and path in ("/info", "/pair-setup", "/pair-verify")
        )
        if not public and not self.pairing.verified:
            resp.status, resp.reason, resp.close = 403, "Forbidden", True
            return resp
        try:
            if req.method == "GET" and path == "/info":
                self.on_info(req, resp)
            elif req.method == "POST" and path == "/pair-setup":
                resp.set(self.pairing.pair_setup(req.body), "application/octet-stream")
            elif req.method == "POST" and path == "/pair-verify":
                resp.set(self.pairing.pair_verify(req.body), "application/octet-stream")
            elif req.method == "POST" and path == "/fp-setup":
                self.on_fp_setup(req, resp)
            elif req.method == "OPTIONS":
                resp.headers.append(("Public", "SETUP, RECORD, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER"))
            elif req.method == "SETUP":
                await self.on_setup(req, resp)
            elif req.method == "GET_PARAMETER":
                self.on_get_parameter(req, resp)
            elif req.method == "SET_PARAMETER":
                self.on_set_parameter(req)
            elif req.method == "RECORD":
                resp.headers.append(("Audio-Latency", "11025"))
                resp.headers.append(("Audio-Jack-Status", "connected; type=analog"))
            elif req.method == "FLUSH":
                if self.audio:
                    self.audio.flush()
            elif req.method == "TEARDOWN":
                await self.on_teardown(req, resp)
            elif req.method == "POST" and path in ("/feedback", "/audioMode"):
                pass
            else:
                log.warning("unhandled request %s %s", req.method, req.url)
        except Exception as e:
            log.exception("error handling %s %s", req.method, req.url)
            resp = Response(500, "Internal Server Error")
            if isinstance(e, ValueError):
                resp = Response(400, "Bad Request")
        return resp

    # ---------------------------------------------------------------- handlers
    def on_info(self, req, resp):
        rx = self.rx
        ct = req.header("Content-Type") or ""
        res = {}
        if "application/x-apple-binary-plist" in ct and req.body:
            try:
                q = plistlib.loads(req.body).get("qualifier") or []
            except Exception:
                q = []
            if "txtAirPlay" in q:
                res["txtAirPlay"] = discovery.txt_bytes(discovery.airplay_txt(rx.settings, rx.pk_hex))
            if "txtRAOP" in q:
                res["txtRAOP"] = discovery.txt_bytes(discovery.raop_txt(rx.pk_hex))
        if not ct:
            s = rx.settings
            res.update(
                {
                    "deviceID": s.device_id,
                    "macAddress": s.device_id,
                    "pk": rx.identity.public,
                    "features": discovery.FEATURES,
                    "name": s["name"],
                    "pi": discovery.PI,
                    "vv": 2,
                    "statusFlags": 68,
                    "keepAliveLowPower": True,
                    "sourceVersion": discovery.SRCVERS,
                    "keepAliveSendStatsAsBody": True,
                    "model": discovery.MODEL,
                }
            )
            if req.header("CSeq") is not None:
                res.update(
                    {
                        "initialVolume": 0.0,
                        "audioLatencies": [
                            {"type": 100, "inputLatencyMicros": 0, "audioType": "default", "outputLatencyMicros": False},
                            {"type": 101, "audioType": "default", "inputLatencyMicros": 0, "outputLatencyMicros": False},
                        ],
                        "audioFormats": [
                            {"audioOutputFormats": 0x3FFFFFC, "type": 100, "audioInputFormats": 0x3FFFFFC},
                            {"audioOutputFormats": 0x3FFFFFC, "type": 101, "audioInputFormats": 0x3FFFFFC},
                        ],
                        "displays": [
                            {
                                "uuid": "e0ff8a27-6738-3d56-8a16-cc53aacee925",
                                "widthPhysical": 0,
                                "heightPhysical": 0,
                                "width": int(s["width"]),
                                "height": int(s["height"]),
                                "widthPixels": int(s["width"]),
                                "heightPixels": int(s["height"]),
                                "rotation": False,
                                "refreshRate": 1.0 / int(s["fps"]),
                                "maxFPS": int(s["fps"]),
                                "overscanned": False,
                                "features": 14,
                            }
                        ],
                    }
                )
        resp.set(_bplist(res), "application/x-apple-binary-plist")

    def on_fp_setup(self, req, resp):
        if len(req.body) == 16:
            if req.body[4] != 0x03:
                log.error("unsupported FairPlay version %d", req.body[4])
                resp.status, resp.reason = 501, "Not Implemented"
                return
            resp.set(self.fairplay.setup(req.body), "application/octet-stream")
        elif len(req.body) == 164:
            resp.set(self.fairplay.handshake(req.body), "application/octet-stream")
        else:
            raise ValueError("invalid fp-setup length")

    async def on_setup(self, req, resp):
        rx = self.rx
        plist = plistlib.loads(req.body) if req.body else {}
        out = {}
        if "ekey" in plist and "eiv" in plist:
            if rx.session_owner not in (None, self):
                resp.status, resp.reason = 453, "Not Enough Bandwidth"
                resp.close = True
                return
            aeskey = await rx.run_io(self.fairplay.decrypt, plist["ekey"])
            if rx.session_owner not in (None, self):
                resp.status, resp.reason = 453, "Not Enough Bandwidth"
                resp.close = True
                return
            rx.session_owner = self
            self.client_name = plist.get("name") or "iPhone"
            log.info("SETUP from %s (%s, %s)", self.client_name, plist.get("model"), plist.get("osVersion"))
            rx.emit("connecting", name=self.client_name, model=plist.get("model") or "",
                    os=plist.get("osVersion") or "")
            if self.pairing.ecdh_secret is not None:
                aeskey = hashlib.sha512(aeskey + self.pairing.ecdh_secret).digest()[:16]
            self.aeskey, self.aesiv = aeskey, plist["eiv"]
            if self.timing:
                self.timing.stop()
            self.timing = TimingSender()
            tport = await self.timing.start(self.remote_ip, int(plist.get("timingPort") or 0))
            out["timingPort"] = tport
            out["eventPort"] = 0
        streams = plist.get("streams")
        if streams:
            if self.aeskey is None:
                raise ValueError("stream SETUP before key exchange")
            res_streams = []
            for s in streams:
                stype = s.get("type")
                if stype == 110:
                    sid = int(s.get("streamConnectionID", 0)) & 0xFFFFFFFFFFFFFFFF
                    if self.mirror:
                        await self.mirror.stop()
                    self.mirror = MirrorStream(self.aeskey, sid, rx.decoder, rx.emit, self.remote_ip)
                    port = await self.mirror.start()
                    res_streams.append({"dataPort": port, "type": 110})
                elif stype == 96:
                    if self.audio:
                        self.audio.stop()
                    ct = int(s.get("ct", 8))
                    player = rx.get_player()
                    self.audio = AudioStream(self.aeskey, self.aesiv, ct, player, self.remote_ip)
                    dport, cport = await self.audio.start()
                    res_streams.append({"dataPort": dport, "controlPort": cport, "type": 96})
                else:
                    log.warning("unknown stream type %s", stype)
            out["streams"] = res_streams
        resp.set(_bplist(out), "application/x-apple-binary-plist")

    def on_get_parameter(self, req, resp):
        if req.body.startswith(b"volume"):
            resp.set(f"volume: {self.rx.volume_db:9.6f}\r\n".encode(), "text/parameters")

    def on_set_parameter(self, req):
        ct = req.header("Content-Type") or ""
        if self.rx.session_owner is self and ct == "text/parameters" and req.body.startswith(b"volume:"):
            try:
                self.rx.set_volume(float(req.body.split(b":", 1)[1].strip()))
            except ValueError:
                pass

    async def on_teardown(self, req, resp):
        types = set()
        if req.body:
            try:
                types = {s.get("type") for s in plistlib.loads(req.body).get("streams", [])}
            except Exception:
                pass
        resp.headers.append(("Connection", "close"))
        if 96 in types and self.audio:
            self.audio.stop()
            self.audio = None
        if 110 in types and self.mirror:
            await self.mirror.stop()
            self.mirror = None
            self.rx.emit("mirror_stopped")
        if not types:
            await self.cleanup()
            resp.close = True

    async def cleanup(self):
        if self.audio:
            self.audio.stop()
            self.audio = None
        if self.mirror:
            await self.mirror.stop()
            self.mirror = None
        if self.timing:
            self.timing.stop()
            self.timing = None
        if self.rx.session_owner is self:
            self.rx.session_owner = None
            self.rx.emit("mirror_stopped")


class Receiver:
    """Runs the AirPlay receiver on its own asyncio thread."""

    def __init__(self, settings, on_frame, on_event):
        self.settings = settings
        self.identity = ServerIdentity(settings["ed25519_seed"])
        self.pk_hex = self.identity.public.hex()
        self.on_event = on_event
        self.decoder = VideoDecoder(on_frame)
        self.player = None
        self.volume_db = 0.0
        self.muted = False
        self.loop = None
        self.port = 0
        self.advertiser = None
        self.session_owner = None
        self._connections = set()
        self._stop_requested = threading.Event()
        self._stopping = None
        self._thread = None
        self._executor = None
        self._server = None

    def emit(self, event, **info):
        try:
            self.on_event(event, info)
        except Exception:
            log.exception("event handler failed")

    # ---------------------------------------------------------------- audio
    def get_player(self):
        if self.player is None:
            self.player = Player(playback=net.ADVERTISE)
            self._apply_volume()
        return self.player

    def set_volume(self, db: float):
        if not math.isfinite(db):
            return
        self.volume_db = max(-144.0, min(0.0, db))
        self._apply_volume()

    def set_muted(self, muted: bool):
        self.muted = muted
        self._apply_volume()

    def _apply_volume(self):
        if self.player is None:
            return
        db = self.volume_db
        self.player.muted = self.muted or db <= -144
        self.player.gain = 0.0 if db <= -144 else max(0.0, min(1.0, 10 ** (db / 20.0)))

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.decoder.start()
        self._thread = threading.Thread(target=self._run, name="airplay", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            # Python 3.15's default asyncio pool can expose an unresolved lazy
            # ThreadPoolExecutor after Qt initializes. Use an explicitly imported
            # class and pass our owned pool directly, without changing stdlib state.
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="airplay-io") as executor:
                self._executor = executor
                try:
                    asyncio.run(self._main())
                finally:
                    self._executor = None
        except OSError as e:
            log.error("AirPlay listener failed: %s", e)
            self.emit("error", message=f"Could not listen on TCP port {net.PORT}. See the log for details.")
        except Exception:
            log.exception("receiver crashed")
            self.emit("error", message="The AirPlay server crashed. See the log for details.")

    async def run_io(self, function, *args):
        if self._executor is None:
            raise RuntimeError("The AirPlay I/O worker has not started")
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    async def _handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if len(self._connections) >= MAX_CONNECTIONS or (
            net.ALLOWED_PHONE is not None and (not peer or peer[0] != net.ALLOWED_PHONE)
        ):
            writer.close()
            return
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            await Connection(self, reader, writer).serve()
        finally:
            self._connections.discard(task)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        try:
            self._server = await asyncio.start_server(self._handle, host=net.BIND_HOST, port=net.PORT,
                                                      limit=MAX_RTSP_HEADER)
            self.port = self._server.sockets[0].getsockname()[1]
            log.info("RTSP server listening on port %d", self.port)
            if net.ADVERTISE:
                self.advertiser = discovery.Advertiser(self.settings, self.pk_hex, self.port)
                self._advertisement_lock = asyncio.Lock()
                await self._announce()
            else:
                self.emit("listening", name=self.settings["name"], addresses=[net.BIND_HOST], port=self.port)
            if not self._stop_requested.is_set():
                await self._stopping.wait()
        finally:
            if self._server is not None:
                self._server.close()
            connections = list(self._connections)
            for task in connections:
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            if self._server is not None:
                await self._server.wait_closed()
            if self.advertiser is not None:
                async with self._advertisement_lock:
                    await self.run_io(self.advertiser.stop)
            if self.player is not None:
                self.player.close()

    async def _announce(self):
        async with self._advertisement_lock:
            if self._stop_requested.is_set():
                return
            try:
                await self.run_io(self.advertiser.stop)
                await self.run_io(self.advertiser.start)
                self.emit(
                    "listening", name=self.settings["name"], addresses=self.advertiser.addresses, port=self.port
                )
            except Exception:
                log.exception("mDNS advertisement failed")
                self.emit("error", message="Could not announce this computer on the network (mDNS).")

    def rename(self, name: str):
        """Change the name shown on the iPhone and re-announce the receiver."""
        discovery.validate_name(name)
        self.settings["name"] = name
        self.settings.save()
        if self.loop and self.loop.is_running() and self.advertiser:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._announce()))

    def stop(self):
        self._stop_requested.set()
        if self.loop and self.loop.is_running() and self._stopping is not None:
            self.loop.call_soon_threadsafe(self._stopping.set)
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning("AirPlay receiver shutdown timed out")
        self.decoder.stop()
