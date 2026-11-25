"""HM30 link packet protocol (PC side).

Frame layout (must match firmware/link_proto.c):

    0xAA 0x55 | type | seq | len | payload[len] | crc_lo crc_hi

* type    1 byte, see TYPE_* below
* seq     1 byte, increments per frame (0..255) so the receiver can count lost frames
* len     1 byte, payload length, 0..MAX_PAYLOAD
* crc     CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over type, seq, len and payload,
          sent little-endian

Payloads:

* TEXT    UTF-8 text
* VALUE   float32 little-endian, followed by the value name in UTF-8 (e.g. "temp")
* BYTES   raw bytes
* CMD     UTF-8 command text (PC -> STM32)
* ACK     [seq, type] of a packet that was accepted (sent back automatically)
* HELLO   sent on connect: [please-answer flag, session id]; an app answers with its own HELLO
* FILE_START  file id, flags, original size, sent size, CRC-32 of the original, name
* FILE_DATA   file id, offset (into the sent bytes), up to FILE_DATA_MAX bytes
* FILE_END    file id

Files are optionally zlib-compressed (flag FILE_ZLIB) before sending; the receiver
decompresses and checks the CRC-32 of the original before saving.

Bytes that arrive outside a frame are collected as plain text lines, so firmware that
just prints text (printf / HAL_UART_Transmit of a string) also shows up in the app.
"""

from __future__ import annotations

import binascii
import os
import re
import struct
import zlib
from dataclasses import dataclass, field

SYNC1 = 0xAA
SYNC2 = 0x55
MAX_PAYLOAD = 240

TYPE_TEXT = 0x01
TYPE_VALUE = 0x02
TYPE_BYTES = 0x03
TYPE_CMD = 0x04
TYPE_ACK = 0x05    # STM32 -> PC: payload [seq, type] of a PC packet it accepted
TYPE_HELLO = 0x06  # on connect: resets the other side's duplicate filter
TYPE_FILE_START = 0x07
TYPE_FILE_DATA = 0x08
TYPE_FILE_END = 0x09

TYPE_NAMES = {TYPE_TEXT: "TEXT", TYPE_VALUE: "VALUE", TYPE_BYTES: "BYTES", TYPE_CMD: "CMD",
              TYPE_ACK: "ACK", TYPE_HELLO: "HELLO", TYPE_FILE_START: "FILE_START",
              TYPE_FILE_DATA: "FILE_DATA", TYPE_FILE_END: "FILE_END"}

# Packets that the receiving side confirms with an ACK
RELIABLE_TYPES = (TYPE_TEXT, TYPE_VALUE, TYPE_BYTES, TYPE_CMD, TYPE_HELLO,
                  TYPE_FILE_START, TYPE_FILE_DATA, TYPE_FILE_END)

HELLO_ANSWER, HELLO_PLEASE_ANSWER = 0, 1


def hello_payload(please_answer: bool, session: int) -> bytes:
    """HELLO = [1 if the other side should answer with its own HELLO, session id (4 bytes)].
    The session id lets the receiver tell a re-sent HELLO from a restarted app."""
    return bytes((HELLO_PLEASE_ANSWER if please_answer else HELLO_ANSWER,)) + struct.pack("<I", session)


def parse_hello(payload: bytes) -> tuple[bool, int | None]:
    please = bool(payload) and payload[0] == HELLO_PLEASE_ANSWER
    session = struct.unpack_from("<I", payload, 1)[0] if len(payload) >= 5 else None
    return please, session


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE. crc16(b"123456789") == 0x29B1."""
    return binascii.crc_hqx(data, 0xFFFF)


def encode(ftype: int, payload: bytes, seq: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long ({len(payload)} > {MAX_PAYLOAD})")
    body = bytes((ftype & 0xFF, seq & 0xFF, len(payload))) + payload
    return bytes((SYNC1, SYNC2)) + body + struct.pack("<H", crc16(body))


def encode_text(text: str, seq: int = 0) -> bytes:
    return encode(TYPE_TEXT, text.encode("utf-8")[:MAX_PAYLOAD], seq)


def value_payload(name: str, value: float) -> bytes:
    return struct.pack("<f", value) + name.encode("utf-8")[: MAX_PAYLOAD - 4]


def encode_value(name: str, value: float, seq: int = 0) -> bytes:
    return encode(TYPE_VALUE, value_payload(name, value), seq)


def encode_cmd(text: str, seq: int = 0) -> bytes:
    return encode(TYPE_CMD, text.encode("utf-8")[:MAX_PAYLOAD], seq)


def encode_bytes(data: bytes, seq: int = 0) -> bytes:
    return encode(TYPE_BYTES, data, seq)


def encode_ack(acked_seq: int, acked_type: int, seq: int = 0) -> bytes:
    return encode(TYPE_ACK, bytes((acked_seq & 0xFF, acked_type & 0xFF)), seq)


# ---- helpers for the app's send box ------------------------------------------


def parse_values(text: str) -> list[tuple[str, float]]:
    """"speed=1.5, angle=-30" -> [("speed", 1.5), ("angle", -30.0)]."""
    out = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f'"{part}" is not name=number (example: speed=1.5)')
        name, val = (s.strip() for s in part.split("=", 1))
        if not name:
            raise ValueError(f'"{part}" has no name before "="')
        try:
            out.append((name, float(val)))
        except ValueError:
            raise ValueError(f'"{val}" is not a number (in "{part}")') from None
    if not out:
        raise ValueError("nothing to send (example: speed=1.5)")
    return out


def parse_hex(text: str) -> bytes:
    """"01 02 ff", "0x01,0x02,0xFF", "0102FF" -> b"\\x01\\x02\\xff"."""
    cleaned = text.replace(",", " ").replace(";", " ").replace("0x", " ").replace("0X", " ")
    tokens = cleaned.split()
    if len(tokens) == 1:  # one run of hex digits without separators
        token = tokens[0]
        if len(token) % 2:
            raise ValueError("odd number of hex digits")
        tokens = [token[i:i + 2] for i in range(0, len(token), 2)]
    try:
        data = bytes(int(t, 16) for t in tokens)
    except ValueError:
        raise ValueError(f'not valid hex: "{text}" (example: 01 02 FF)') from None
    if not data:
        raise ValueError("nothing to send (example: 01 02 FF)")
    return data


def chunk(data: bytes, size: int = MAX_PAYLOAD) -> list[bytes]:
    """Split data (e.g. a file) into payload-sized pieces."""
    return [data[i:i + size] for i in range(0, len(data), size)] or [b""]


@dataclass
class Frame:
    ftype: int
    seq: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.ftype, f"0x{self.ftype:02X}")

    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")

    def acked(self) -> tuple[int, int]:
        """For ACK frames: (seq, type) of the PC packet that was accepted."""
        if len(self.payload) < 2:
            raise ValueError("ACK payload shorter than 2 bytes")
        return self.payload[0], self.payload[1]

    def value(self) -> tuple[str, float]:
        """For VALUE frames: (name, value)."""
        if len(self.payload) < 4:
            raise ValueError("VALUE payload shorter than 4 bytes")
        (v,) = struct.unpack("<f", self.payload[:4])
        return self.payload[4:].decode("utf-8", errors="replace"), v


@dataclass
class RawLine:
    """Plain text received outside any frame (one line, newline stripped)."""

    text: str


@dataclass
class ParserStats:
    frames: int = 0
    crc_errors: int = 0
    lost: int = 0  # frames missing according to the seq counter
    raw_bytes: int = 0


class Parser:
    """Byte-stream parser. Feed any chunk size; get Frame / RawLine objects back."""

    _WAIT_SYNC1, _WAIT_SYNC2, _TYPE, _SEQ, _LEN, _PAYLOAD, _CRC = range(7)

    def __init__(self) -> None:
        self.stats = ParserStats()
        self._state = self._WAIT_SYNC1
        self._body = bytearray()
        self._need = 0
        self._crc = bytearray()
        self._raw = bytearray()
        self._last_seq: int | None = None

    def feed(self, data: bytes) -> list[Frame | RawLine]:
        out: list[Frame | RawLine] = []
        for b in data:
            self._step(b, out)
        return out

    def flush_raw(self) -> list[RawLine]:
        """Return a pending raw line that has no newline yet (e.g. on a timeout)."""
        if not self._raw:
            return []
        line = RawLine(self._raw.decode("utf-8", errors="replace").rstrip("\r"))
        self._raw.clear()
        return [line]

    def _raw_byte(self, b: int, out: list) -> None:
        self.stats.raw_bytes += 1
        if b == 0x0A:  # '\n'
            out.append(RawLine(self._raw.decode("utf-8", errors="replace").rstrip("\r")))
            self._raw.clear()
        else:
            self._raw.append(b)
            if len(self._raw) > 1024:
                out.extend(self.flush_raw())

    def _step(self, b: int, out: list) -> None:
        s = self._state
        if s == self._WAIT_SYNC1:
            if b == SYNC1:
                self._state = self._WAIT_SYNC2
            else:
                self._raw_byte(b, out)
        elif s == self._WAIT_SYNC2:
            if b == SYNC2:
                self._body.clear()
                self._crc.clear()
                self._state = self._TYPE
            elif b == SYNC1:
                self._raw_byte(SYNC1, out)  # previous 0xAA was plain data; stay here
            else:
                self._raw_byte(SYNC1, out)
                self._raw_byte(b, out)
                self._state = self._WAIT_SYNC1
        elif s in (self._TYPE, self._SEQ):
            self._body.append(b)
            self._state += 1
        elif s == self._LEN:
            self._body.append(b)
            if b > MAX_PAYLOAD:
                self.stats.crc_errors += 1
                self._state = self._WAIT_SYNC1
            else:
                self._need = b
                self._state = self._PAYLOAD if b else self._CRC
        elif s == self._PAYLOAD:
            self._body.append(b)
            self._need -= 1
            if self._need == 0:
                self._state = self._CRC
        elif s == self._CRC:
            self._crc.append(b)
            if len(self._crc) == 2:
                self._state = self._WAIT_SYNC1
                (rx_crc,) = struct.unpack("<H", self._crc)
                if rx_crc != crc16(bytes(self._body)):
                    self.stats.crc_errors += 1
                    return
                ftype, seq = self._body[0], self._body[1]
                if self._last_seq is not None:
                    gap = (seq - self._last_seq - 1) & 0xFF
                    if gap < 128:  # ignore huge jumps (e.g. STM32 reset)
                        self.stats.lost += gap
                self._last_seq = seq
                self.stats.frames += 1
                out.append(Frame(ftype, seq, bytes(self._body[3:])))


# ---- files ----------------------------------------------------------------------

FILE_ZLIB = 0x01
_FILE_START_HDR = struct.Struct("<BBIII")   # id, flags, original size, sent size, crc32
_FILE_DATA_HDR = struct.Struct("<BI")       # id, offset
FILE_DATA_MAX = MAX_PAYLOAD - _FILE_DATA_HDR.size  # 235 bytes of file per packet
FILE_NAME_MAX = MAX_PAYLOAD - _FILE_START_HDR.size

# Already-compressed formats: zlib would only waste time on these.
NO_COMPRESS_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".zip", ".7z", ".rar", ".gz", ".bz2",
    ".xz", ".zst", ".mp3", ".aac", ".ogg", ".flac", ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".pdf", ".docx", ".xlsx", ".pptx", ".apk", ".jar",
}


@dataclass
class OutgoingFile:
    name: str
    original: bytes
    body: bytes          # what actually goes over the radio (maybe compressed)
    flags: int
    crc: int             # CRC-32 of the original

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FILE_ZLIB)


def prepare_file(name: str, data: bytes) -> OutgoingFile:
    """Compress the file if that makes it at least 10 % smaller."""
    body, flags = data, 0
    if os.path.splitext(name)[1].lower() not in NO_COMPRESS_EXT and len(data) > 64:
        packed = zlib.compress(data, 6)
        if len(packed) <= 0.9 * len(data):
            body, flags = packed, FILE_ZLIB
    return OutgoingFile(name, data, body, flags, zlib.crc32(data))


def file_start_payload(fid: int, f: OutgoingFile) -> bytes:
    name = f.name.encode("utf-8")[:FILE_NAME_MAX]
    return _FILE_START_HDR.pack(fid & 0xFF, f.flags, len(f.original), len(f.body), f.crc) + name


def file_data_payload(fid: int, offset: int, data: bytes) -> bytes:
    return _FILE_DATA_HDR.pack(fid & 0xFF, offset) + data


def file_pieces(body: bytes) -> list[tuple[int, bytes]]:
    """(offset, data) pieces that fit into FILE_DATA packets."""
    return [(o, body[o:o + FILE_DATA_MAX]) for o in range(0, len(body), FILE_DATA_MAX)]


def safe_filename(name: str) -> str:
    """Keep only the base name and drop characters Windows does not allow."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "received_file"


def unique_path(folder: str, name: str) -> str:
    """folder/name, or folder/name (1).ext, (2)... so nothing is overwritten."""
    base, ext = os.path.splitext(safe_filename(name))
    path, n = os.path.join(folder, base + ext), 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


class FileError(Exception):
    pass


@dataclass
class IncomingFile:
    """Collects FILE_DATA pieces of one file and checks it at the end."""

    fid: int
    name: str
    flags: int
    original_size: int
    sent_size: int
    crc: int
    buf: bytearray = field(default_factory=bytearray)
    got: set = field(default_factory=set)        # offsets received
    received: int = 0

    @classmethod
    def from_start(cls, payload: bytes) -> "IncomingFile":
        if len(payload) < _FILE_START_HDR.size:
            raise FileError("FILE_START too short")
        fid, flags, osize, ssize, crc = _FILE_START_HDR.unpack_from(payload)
        name = safe_filename(payload[_FILE_START_HDR.size:].decode("utf-8", errors="replace"))
        return cls(fid, name, flags, osize, ssize, crc, bytearray(ssize))

    def add(self, payload: bytes) -> int:
        """Store one FILE_DATA payload. Returns the file id it belongs to."""
        fid, offset = _FILE_DATA_HDR.unpack_from(payload)
        data = payload[_FILE_DATA_HDR.size:]
        if offset + len(data) > self.sent_size:
            raise FileError("piece beyond the end of the file")
        if offset not in self.got:
            self.buf[offset:offset + len(data)] = data
            self.got.add(offset)
            self.received += len(data)
        return fid

    @property
    def fraction(self) -> float:
        return self.received / self.sent_size if self.sent_size else 1.0

    def finish(self) -> bytes:
        """The original file content; raises FileError if anything is missing or wrong."""
        if self.received != self.sent_size:
            raise FileError(f"incomplete: {self.received:,} of {self.sent_size:,} bytes")
        data = bytes(self.buf)
        if self.flags & FILE_ZLIB:
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise FileError(f"cannot decompress: {exc}") from None
        if len(data) != self.original_size or zlib.crc32(data) != self.crc:
            raise FileError("check failed (CRC-32 does not match the original)")
        return data


def file_data_id(payload: bytes) -> int:
    return payload[0] if payload else -1
