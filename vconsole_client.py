"""
vconsole_client.py — Source 2 VConsole2 protocol client (Python 3.11+).

Target: Valve's Source 2 engine remote console (default TCP 127.0.0.1:29000).
Verified against Deadlock (Steam AppID 1422450) and CS2; protocol is shared.

Packet framing (all multi-byte integers big-endian):

    offset 0  | type    | 4 ASCII bytes  ("CMND", "PRNT", "CHAN", ...)
    offset 4  | version | uint32
    offset 8  | length  | uint16  -- TOTAL packet length incl. the 12-byte header
    offset 10 | handle  | uint16
    offset 12 | body    | (length - 12) bytes

Reference: theokyr/CS2RemoteConsole libvconsole (VConChunk = 12 bytes;
readChunk reads header, resizes buffer to `header.length`, then reads
`header.length - sizeof(VConChunk)` body bytes).

Single-file, stdlib-only. See VCONSOLE_NOTES.md for protocol tables.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

HEADER_SIZE = 12  # 4 type + 4 version + 2 length + 2 handle
HEADER_STRUCT = struct.Struct(">4sIHH")  # big-endian

# Version byte used in CMND packets. The modern CS2 client library uses 0xD4;
# the older 3rd-party Py2 port used 0xD2. Both are accepted by the game — the
# high byte appears to be a protocol minor version tag. Caller can override.
CMND_VERSION_DEFAULT = 0x00D40000  # wire bytes: 00 D4 00 00 (Penguinwizzard htonl pattern). D2 = 0x00D20000 отвергается.

# Known inbound packet types we parse (everything else is drained + discarded).
PKT_AINF = b"AINF"
PKT_PRNT = b"PRNT"
PKT_CHAN = b"CHAN"
PKT_CVAR = b"CVAR"
PKT_CFGV = b"CFGV"
PKT_ADON = b"ADON"
PKT_CMND = b"CMND"

_KNOWN_TYPES = {PKT_AINF, PKT_PRNT, PKT_CHAN, PKT_CVAR, PKT_CFGV, PKT_ADON, PKT_CMND}


# ---------------------------------------------------------------------------
# Parsed message dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Channel:
    id: int
    unknown1: int
    unknown2: int
    verbosity_default: int
    verbosity_current: int
    rgba_override: int  # uint32, packed RGBA
    name: str


@dataclass
class Cvar:
    name: str
    unknown: int
    flags: int
    rangemin: float
    rangemax: float


@dataclass
class PrntMsg:
    channel_id: int
    color: int  # uint32 RGBA, taken from bytes [12:16] of the body per C++ ref
    message: str


@dataclass
class RawPacket:
    msg_type: bytes       # 4-byte ASCII tag
    version: int
    length: int           # total packet length (header + body)
    handle: int
    body: bytes           # exactly (length - 12) bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_non_ascii(b: bytes) -> str:
    """Mirror the C++ reference's stripNonAscii on PRNT messages."""
    # Trim at first NUL (C-string) then keep only 7-bit ASCII printables + common WS.
    nul = b.find(b"\x00")
    if nul >= 0:
        b = b[:nul]
    return bytes(c for c in b if c <= 127).decode("ascii", errors="replace")


def _cstr(b: bytes) -> str:
    """Read a NUL-terminated fixed-width field as UTF-8 (best-effort)."""
    nul = b.find(b"\x00")
    if nul >= 0:
        b = b[:nul]
    return b.decode("utf-8", errors="replace")


def build_cmnd_payload(cmd: str, version: int = CMND_VERSION_DEFAULT) -> bytes:
    """
    Build a CMND packet. Layout (matches libvconsole createCommandPayload):

        'C' 'M' 'N' 'D'              (4 bytes)
        version                       (uint32 BE)
        total_length                  (uint16 BE) -- incl. header + cmd + NUL
        handle = 0                    (uint16 BE)
        cmd utf-8                     (N bytes)
        NUL                           (1 byte)

    Raises ValueError if the encoded command does not fit in uint16.
    """
    cmd_bytes = cmd.encode("utf-8")
    total_length = HEADER_SIZE + len(cmd_bytes) + 1
    if total_length > 0xFFFF:
        raise ValueError(
            f"command too long: {len(cmd_bytes)} bytes (max ~{0xFFFF - HEADER_SIZE - 1})"
        )
    header = HEADER_STRUCT.pack(b"CMND", version, total_length, 0)
    return header + cmd_bytes + b"\x00"


# ---------------------------------------------------------------------------
# Parsers (body-only — header already consumed)
# ---------------------------------------------------------------------------


def parse_prnt(body: bytes) -> PrntMsg:
    """
    PRNT body layout (per libvconsole):
        int32  channel_id
        bytes  unknown[20]     (color sits within this at offset +8)
        ...
        offset 28: NUL-terminated message
    """
    if len(body) < 28:
        raise ValueError(f"PRNT body too short: {len(body)} bytes")
    channel_id = struct.unpack(">i", body[0:4])[0]
    # C++ memcpys prnt.color from data + 12, i.e. body offset 12..16.
    color = struct.unpack(">I", body[12:16])[0]
    message = _strip_non_ascii(body[28:])
    return PrntMsg(channel_id=channel_id, color=color, message=message)


def parse_chan(body: bytes) -> list[Channel]:
    """
    CHAN body: uint16 numChannels (often overridden by size), then N * 54-byte
    Channel records:
        int32 id, int32 u1, int32 u2, int32 verb_default, int32 verb_current,
        uint32 rgba, char name[34]
    """
    CHANNEL_REC_SIZE = 4 * 5 + 4 + 34  # = 58? -- let's recompute
    # 5 * int32 (20) + uint32 rgba (4) + 34 name = 58 bytes. The C++ header
    # prefix is just a uint16; remaining / 58 gives count.
    if len(body) < 2:
        return []
    prefix = struct.unpack(">H", body[0:2])[0]
    rest = body[2:]
    rec = 58
    count = len(rest) // rec
    # Keep `prefix` only as a sanity hint; trust derived count as C++ does.
    _ = prefix
    out: list[Channel] = []
    for i in range(count):
        off = i * rec
        id_, u1, u2, vd, vc, rgba = struct.unpack(">iiiiiI", rest[off:off + 24])
        name = _cstr(rest[off + 24:off + 24 + 34])
        out.append(Channel(id_, u1, u2, vd, vc, rgba, name))
    return out


def parse_cvar(body: bytes) -> Cvar:
    """
    CVAR body: char[64] name, uint32 unknown, uint32 flags, float rangemin,
    float rangemax, uint8 padding. All multi-byte fields big-endian.
    """
    if len(body) < 64 + 4 + 4 + 4 + 4 + 1:
        raise ValueError(f"CVAR body too short: {len(body)} bytes")
    name = _cstr(body[0:64])
    unknown, flags, rmin, rmax = struct.unpack(">IIff", body[64:64 + 16])
    return Cvar(name=name, unknown=unknown, flags=flags, rangemin=rmin, rangemax=rmax)


def parse_adon(body: bytes) -> str:
    """ADON body: uint16 unknown, uint16 name_len, char[name_len] name."""
    if len(body) < 4:
        return ""
    _unknown, name_len = struct.unpack(">HH", body[0:4])
    name = body[4:4 + name_len]
    return _cstr(name) if b"\x00" in name else name.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class VConsoleError(Exception):
    """Raised for protocol / transport errors."""


class VConsoleClient:
    """
    Synchronous connect + background listener thread.

    Usage:

        client = VConsoleClient(on_print=lambda ch, msg: print(f"[{ch}] {msg}"))
        client.connect("127.0.0.1", 29000)
        client.send_command("echo hello")
        ...
        client.disconnect()
    """

    def __init__(
        self,
        on_print: Optional[Callable[[str, str], None]] = None,
        on_channels: Optional[Callable[[list[Channel]], None]] = None,
        on_cvar: Optional[Callable[[Cvar], None]] = None,
        on_addon: Optional[Callable[[str], None]] = None,
        on_packet: Optional[Callable[[RawPacket], None]] = None,
        on_disconnect: Optional[Callable[[Optional[BaseException]], None]] = None,
        cmnd_version: int = CMND_VERSION_DEFAULT,
    ) -> None:
        self.on_print = on_print
        self.on_channels = on_channels
        self.on_cvar = on_cvar
        self.on_addon = on_addon
        self.on_packet = on_packet  # raw firehose (called BEFORE typed callback)
        self.on_disconnect = on_disconnect
        self.cmnd_version = cmnd_version

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._listener: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._channels: list[Channel] = []
        self._channels_lock = threading.Lock()

    # ---- lifecycle -----------------------------------------------------

    def connect(self, host: str = "127.0.0.1", port: int = 29000, timeout: float = 5.0) -> None:
        if self._sock is not None:
            raise VConsoleError("already connected")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
        except OSError as e:
            s.close()
            raise VConsoleError(f"connect to {host}:{port} failed: {e}") from e
        s.settimeout(None)  # switch to blocking for the listener
        self._sock = s
        self._stop.clear()
        self._listener = threading.Thread(
            target=self._listen_loop,
            name="vconsole-listener",
            daemon=True,
        )
        self._listener.start()

    def disconnect(self) -> None:
        """Idempotent graceful shutdown."""
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        t = self._listener
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._listener = None

    def __enter__(self) -> "VConsoleClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ---- outbound ------------------------------------------------------

    def send_command(self, cmd: str) -> None:
        """Thread-safe send of a CMND packet."""
        sock = self._sock
        if sock is None:
            raise VConsoleError("not connected")
        payload = build_cmnd_payload(cmd, version=self.cmnd_version)
        with self._send_lock:
            try:
                sock.sendall(payload)
            except OSError as e:
                raise VConsoleError(f"send failed: {e}") from e

    # ---- inbound -------------------------------------------------------

    @property
    def channels(self) -> list[Channel]:
        with self._channels_lock:
            return list(self._channels)

    def _channel_name(self, cid: int) -> str:
        with self._channels_lock:
            for ch in self._channels:
                if ch.id == cid:
                    return ch.name
        return f"unknown:{cid}"

    def _recv_exact(self, n: int) -> bytes:
        """
        Read exactly n bytes from the socket. Returns n bytes, or raises
        ConnectionError on clean close / short read.

        This is THE bug in the old Py2 port: it assumed recv(n) returned n
        bytes. TCP does not promise that — recv can return any positive
        number up to n, and returning 0 means EOF. Calling struct.unpack on
        a short read blew up with "negative buffersize" once fragmentation
        kicked in under real game traffic.
        """
        if n < 0:
            raise ValueError(f"recv_exact: negative n={n}")
        if n == 0:
            return b""
        sock = self._sock
        if sock is None:
            raise ConnectionError("socket closed")
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            try:
                chunk = sock.recv_into(view[got:], n - got)
            except OSError as e:
                raise ConnectionError(f"recv error: {e}") from e
            if chunk == 0:
                raise ConnectionError(f"peer closed after {got}/{n} bytes")
            got += chunk
        return bytes(buf)

    def _listen_loop(self) -> None:
        err: Optional[BaseException] = None
        try:
            while not self._stop.is_set():
                header = self._recv_exact(HEADER_SIZE)
                msg_type, version, length, handle = HEADER_STRUCT.unpack(header)
                if length < HEADER_SIZE:
                    raise VConsoleError(
                        f"invalid packet length {length} for type {msg_type!r} "
                        f"(must be >= {HEADER_SIZE})"
                    )
                body = self._recv_exact(length - HEADER_SIZE)
                pkt = RawPacket(
                    msg_type=msg_type,
                    version=version,
                    length=length,
                    handle=handle,
                    body=body,
                )
                self._dispatch(pkt)
        except ConnectionError:
            pass  # clean / remote close
        except Exception as e:  # noqa: BLE001 -- we surface it to callback
            err = e
        finally:
            if self.on_disconnect is not None:
                try:
                    self.on_disconnect(err)
                except Exception:  # noqa: BLE001 -- callback must not kill thread
                    pass

    def _dispatch(self, pkt: RawPacket) -> None:
        # Firehose first, in case caller wants to log/tee unknowns.
        if self.on_packet is not None:
            try:
                self.on_packet(pkt)
            except Exception:  # noqa: BLE001
                pass

        mt = pkt.msg_type

        if mt == PKT_PRNT:
            try:
                prnt = parse_prnt(pkt.body)
            except Exception:  # noqa: BLE001
                return
            if self.on_print is not None:
                self.on_print(self._channel_name(prnt.channel_id), prnt.message)

        elif mt == PKT_CHAN:
            chans = parse_chan(pkt.body)
            with self._channels_lock:
                self._channels = chans
            if self.on_channels is not None:
                self.on_channels(chans)

        elif mt == PKT_CVAR:
            try:
                cv = parse_cvar(pkt.body)
            except Exception:  # noqa: BLE001
                return
            if self.on_cvar is not None:
                self.on_cvar(cv)

        elif mt == PKT_ADON:
            name = parse_adon(pkt.body)
            if self.on_addon is not None:
                self.on_addon(name)

        elif mt in _KNOWN_TYPES:
            # AINF, CFGV, CMND — recognized but we don't expose a typed callback.
            # Body already consumed via _recv_exact, so nothing else to do.
            pass
        # Unknown types: body already drained by _recv_exact(length - 12).


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def _demo() -> None:
    import sys
    import time

    def on_print(channel: str, message: str) -> None:
        sys.stdout.write(f"[{channel}] {message}")
        if not message.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    def on_channels(chans: list[Channel]) -> None:
        print(f"[vconsole] learned {len(chans)} channels", file=sys.stderr)

    def on_addon(name: str) -> None:
        print(f"[vconsole] addon: {name}", file=sys.stderr)

    def on_disconnect(err: Optional[BaseException]) -> None:
        print(f"[vconsole] disconnected (err={err!r})", file=sys.stderr)

    client = VConsoleClient(
        on_print=on_print,
        on_channels=on_channels,
        on_addon=on_addon,
        on_disconnect=on_disconnect,
    )
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 29000

    print(f"[vconsole] connecting to {host}:{port} ...", file=sys.stderr)
    client.connect(host, port)
    print("[vconsole] connected. Type commands; Ctrl-C to quit.", file=sys.stderr)

    try:
        # Probe: ask for the player's in-game name (same bootstrap as the C++ ref).
        time.sleep(0.2)
        client.send_command("name")
        # Then hand off to stdin.
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            if not line:
                continue
            client.send_command(line)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()


if __name__ == "__main__":
    _demo()
