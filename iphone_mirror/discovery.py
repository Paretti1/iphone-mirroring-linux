"""Bonjour / mDNS advertisement of the receiver (_airplay._tcp and _raop._tcp)."""
import logging
import socket
from ipaddress import IPv4Address

import ifaddr

from zeroconf import IPVersion, ServiceInfo, Zeroconf

from . import net

log = logging.getLogger(__name__)

MODEL = "AppleTV3,2"
SRCVERS = "220.68"
FEATURES = 0x5A7FFEE6  # legacy pairing on, no HomeKit pairing, H.264 mirroring
FEATURES_STR = f"0x{FEATURES & 0xFFFFFFFF:X},0x{FEATURES >> 32:X}"
PI = "2e388006-13ba-4041-9a67-25dd4a43d536"


def airplay_txt(settings, pk_hex: str) -> dict:
    return {
        "deviceid": settings.device_id,
        "features": FEATURES_STR,
        "flags": "0x4",
        "model": MODEL,
        "pk": pk_hex,
        "pi": PI,
        "srcvers": SRCVERS,
        "vv": "2",
        "pw": "false",
    }


def raop_txt(pk_hex: str) -> dict:
    return {
        "ch": "2",
        "cn": "0,1,2,3",
        "da": "true",
        "et": "0,3,5",
        "vv": "2",
        "ft": FEATURES_STR,
        "am": MODEL,
        "md": "0,1,2",
        "rhd": "5.6.0.0",
        "pw": "false",
        "sf": "0x4",
        "sr": "44100",
        "ss": "16",
        "sv": "false",
        "tp": "UDP",
        "txtvers": "1",
        "vs": SRCVERS,
        "vn": "65537",
        "pk": pk_hex,
    }


def txt_bytes(txt: dict) -> bytes:
    """DNS TXT rdata encoding (used in GET /info "txtAirPlay")."""
    out = bytearray()
    for k, v in txt.items():
        item = f"{k}={v}".encode()
        out.append(len(item))
        out += item
    return bytes(out)


def local_ipv4_addresses():
    if net.BIND_HOST != "0.0.0.0":
        return [net.BIND_HOST]
    addresses = set()
    for adapter in ifaddr.get_adapters():
        for info in adapter.ips:
            if not info.is_IPv4:
                continue
            address = IPv4Address(info.ip)
            if not (address.is_loopback or address.is_link_local or address.is_unspecified):
                addresses.add(address)
    return [str(address) for address in sorted(addresses)]


def validate_name(name):
    # RAOP's instance name includes the hexadecimal receiver ID and an @ sign.
    if not name or "\x00" in name or len(name.encode("utf-8")) > 50:
        raise ValueError("Use a network name between 1 and 50 UTF-8 bytes.")


class Advertiser:
    def __init__(self, settings, pk_hex: str, port: int):
        self.settings = settings
        self.pk_hex = pk_hex
        self.port = port
        self.zc = None
        self.infos = []
        self.addresses = []

    def start(self):
        self.addresses = local_ipv4_addresses()
        name = self.settings["name"]
        validate_name(name)
        if not self.addresses:
            raise OSError("no non-loopback IPv4 network interface")
        host = socket.gethostname().split(".")[0] + ".local."
        self.zc = Zeroconf(interfaces=self.addresses, ip_version=IPVersion.V4Only)
        infos = [
            ServiceInfo(
                "_airplay._tcp.local.",
                f"{name}._airplay._tcp.local.",
                port=self.port,
                properties=airplay_txt(self.settings, self.pk_hex),
                server=host,
                parsed_addresses=self.addresses or None,
            ),
            ServiceInfo(
                "_raop._tcp.local.",
                f"{self.settings.raop_prefix}@{name}._raop._tcp.local.",
                port=self.port,
                properties=raop_txt(self.pk_hex),
                server=host,
                parsed_addresses=self.addresses or None,
            ),
        ]
        for info in infos:
            self.zc.register_service(info, allow_name_change=True)
            self.infos.append(info)
        log.info("advertised '%s' on %s port %d", name, ", ".join(self.addresses) or "?", self.port)

    def stop(self):
        if self.zc:
            try:
                for info in self.infos:
                    self.zc.unregister_service(info)
            finally:
                self.zc.close()
                self.zc = None
                self.infos.clear()
