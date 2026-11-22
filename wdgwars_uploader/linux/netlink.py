"""Minimal netlink client: generic netlink (nl80211) and rtnetlink, no dependencies.

Only what the Wi-Fi scanner and monitor mode need: resolve a generic netlink
family, send a request (optionally a dump), collect the replies, and walk the
attributes. Errors come back as :class:`NetlinkError` carrying the errno, so a
caller can tell "not permitted" (EPERM) from "busy" (EBUSY) and so on.
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import threading
from typing import Iterator

NETLINK_ROUTE = 0
NETLINK_GENERIC = 16

NLM_F_REQUEST = 0x1
NLM_F_MULTI = 0x2
NLM_F_ACK = 0x4
NLM_F_DUMP = 0x300

NLMSG_ERROR = 2
NLMSG_DONE = 3

NLA_F_NESTED = 0x8000
NLA_TYPE_MASK = 0x3FFF

GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2

RTM_NEWLINK = 16
RTM_GETLINK = 18
IFF_UP = 0x1

_HDR = struct.Struct("=IHHII")      # nlmsghdr
_GENL = struct.Struct("=BBH")        # genlmsghdr
_ATTR = struct.Struct("=HH")         # nlattr
_IFINFO = struct.Struct("=BxHiII")   # ifinfomsg


class NetlinkError(OSError):
    pass


def _align(n: int) -> int:
    return (n + 3) & ~3


# -- attributes ------------------------------------------------------------------
def attr(kind: int, payload: bytes, nested: bool = False) -> bytes:
    hdr = _ATTR.pack(_ATTR.size + len(payload), kind | (NLA_F_NESTED if nested else 0))
    raw = hdr + payload
    return raw + b"\0" * (_align(len(raw)) - len(raw))


def attr_u32(kind: int, v: int) -> bytes:
    return attr(kind, struct.pack("=I", v & 0xFFFFFFFF))


def attr_str(kind: int, s: str) -> bytes:
    return attr(kind, s.encode() + b"\0")


def attr_flag(kind: int) -> bytes:
    return attr(kind, b"")


def parse_attrs(buf: bytes, off: int = 0, end: int | None = None) -> dict[int, bytes]:
    """Attributes in ``buf[off:end]`` as ``{type: payload}`` (last one wins)."""
    out: dict[int, bytes] = {}
    for kind, payload in iter_attrs(buf, off, end):
        out[kind] = payload
    return out


def iter_attrs(buf: bytes, off: int = 0, end: int | None = None) -> Iterator[tuple[int, bytes]]:
    end = len(buf) if end is None else end
    while off + _ATTR.size <= end:
        ln, kind = _ATTR.unpack_from(buf, off)
        if ln < _ATTR.size or off + ln > end:
            break
        yield kind & NLA_TYPE_MASK, buf[off + _ATTR.size:off + ln]
        off += _align(ln)


def u8(b: bytes | None, default: int = 0) -> int:
    return b[0] if b else default


def u16(b: bytes | None, default: int = 0) -> int:
    return struct.unpack_from("=H", b)[0] if b and len(b) >= 2 else default


def u32(b: bytes | None, default: int = 0) -> int:
    return struct.unpack_from("=I", b)[0] if b and len(b) >= 4 else default


def s32(b: bytes | None, default: int = 0) -> int:
    return struct.unpack_from("=i", b)[0] if b and len(b) >= 4 else default


def u64(b: bytes | None, default: int = 0) -> int:
    return struct.unpack_from("=Q", b)[0] if b and len(b) >= 8 else default


def cstr(b: bytes | None) -> str:
    return (b or b"").split(b"\0", 1)[0].decode("utf-8", "replace")


# -- sockets ---------------------------------------------------------------------
class _Socket:
    def __init__(self, proto: int) -> None:
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, proto)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.bind((0, 0))
        self.sock.settimeout(5.0)
        self._seq = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def request(self, msg_type: int, flags: int, body: bytes) -> list[bytes]:
        """Send one request and return the payload (after nlmsghdr) of every reply
        up to the ACK or the end of the dump."""
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
            flags |= NLM_F_REQUEST | NLM_F_ACK
            self.sock.send(_HDR.pack(_HDR.size + len(body), msg_type, flags, seq, 0) + body)
            out: list[bytes] = []
            while True:
                data = self.sock.recv(1 << 20)
                off = 0
                while off + _HDR.size <= len(data):
                    ln, kind, fl, rseq, _pid = _HDR.unpack_from(data, off)
                    if ln < _HDR.size:
                        return out
                    payload = data[off + _HDR.size:off + ln]
                    off += _align(ln)
                    if rseq != seq:
                        continue
                    if kind == NLMSG_ERROR:
                        code = -struct.unpack_from("=i", payload)[0]
                        if code:
                            raise NetlinkError(code, os.strerror(code))
                        return out      # the ACK
                    if kind == NLMSG_DONE:
                        return out
                    out.append(payload)


class Genl(_Socket):
    """A generic netlink socket for one family (e.g. ``Genl("nl80211")``)."""

    def __init__(self, family: str) -> None:
        super().__init__(NETLINK_GENERIC)
        try:
            self.family = self._resolve(family)
        except Exception:
            self.close()
            raise

    def _resolve(self, name: str) -> int:
        body = _GENL.pack(CTRL_CMD_GETFAMILY, 1, 0) + attr_str(CTRL_ATTR_FAMILY_NAME, name)
        try:
            replies = super().request(GENL_ID_CTRL, 0, body)
        except NetlinkError as e:
            if e.errno == errno.ENOENT:
                raise NetlinkError(e.errno, f"no {name} support in this kernel") from None
            raise
        for r in replies:
            fid = u16(parse_attrs(r, _GENL.size).get(CTRL_ATTR_FAMILY_ID))
            if fid:
                return fid
        raise NetlinkError(errno.ENOENT, f"no {name} support in this kernel")

    def call(self, cmd: int, attrs: bytes = b"", dump: bool = False) -> list[dict[int, bytes]]:
        """Run a command; each reply comes back as its attribute dict."""
        body = _GENL.pack(cmd, 0, 0) + attrs
        replies = self.request(self.family, NLM_F_DUMP if dump else 0, body)
        return [parse_attrs(r, _GENL.size) for r in replies]


class Rtnl(_Socket):
    def __init__(self) -> None:
        super().__init__(NETLINK_ROUTE)

    def set_up(self, ifindex: int, up: bool) -> None:
        body = _IFINFO.pack(socket.AF_UNSPEC, 0, ifindex, IFF_UP if up else 0, IFF_UP)
        self.request(RTM_NEWLINK, 0, body)

    def links(self) -> list[tuple[int, int, str]]:
        """``(ifindex, flags, name)`` for every network interface."""
        out = []
        for r in self.request(RTM_GETLINK, NLM_F_DUMP, _IFINFO.pack(socket.AF_UNSPEC, 0, 0, 0, 0)):
            _fam, _type, idx, flags, _chg = _IFINFO.unpack_from(r)
            name = cstr(parse_attrs(r, _IFINFO.size).get(3))   # IFLA_IFNAME
            out.append((idx, flags, name))
        return out
