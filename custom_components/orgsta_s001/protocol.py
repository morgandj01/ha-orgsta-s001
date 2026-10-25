"""Wire protocol for the ORGSTA S001 (Snap & Tag) label printer.

The framing, command numbers, media recipes and status layout are ported from
TiMini Print (https://github.com/Dejniel/TiMini-Print), Apache License 2.0,
family "yk_astra_p1", variant "s001". See NOTICE in the repository root.

Frame: 0x64 | cmd | seq | len (u16 LE) | payload | integrity (u32 LE) | 0x9B
The printer accepts integrity = 0; replies carry
0x12345678 + sum(bytes before integrity) + 0x9B.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

FRAME_START = 0x64
FRAME_END = 0x9B
SEQUENCE_MODULUS = 64
INTEGRITY_SEED = 0x12345678

HEAD_WIDTH = 96
ROW_BYTES = HEAD_WIDTH // 8
RASTER_LEFT_PADDING = 6
ROWS_PER_SLICE = 4
DEFAULT_SPEED = 25
DEFAULT_DENSITY = 9
# carta a etichette: quanto avanza dopo il buco prima di stampare. Era 2 e l'etichetta 12x40 (280 righe su circa 320)
# usciva tutta verso un lato; 24 la centra (misurata su «Semi di lino», 21 ottobre 2026). Si ritocca con
# «Spostamento stampa» (8 punti = 1 mm)
TAG_START = 24


class Cmd(IntEnum):
    RASTER = 0x00
    FEED_FORWARD = 0x02
    FEED_SPECIAL = 0x03
    FEED_BACKWARD = 0x04
    DENSITY = 0x09
    SPEED = 0x0A
    STATUS_QUERY = 0x10
    PAPER_TYPE = 0x28
    PAPER_QUERY = 0x72
    PAPER_REPLY = 0x73


STATUS_REPLY_COMMANDS = (0x80, 0x81, 0xFF)


class PaperMode:
    TAG = "tag"
    PLAIN = "plain"
    BLACK_TAG = "black_tag"


def pack_frame(cmd: int, payload: bytes = b"", seq: int = 0) -> bytes:
    body = bytes(payload)
    head = bytes((FRAME_START, cmd & 0xFF, seq % SEQUENCE_MODULUS)) + len(body).to_bytes(2, "little")
    return head + body + b"\x00\x00\x00\x00" + bytes((FRAME_END,))


@dataclass(frozen=True)
class Frame:
    cmd: int
    seq: int
    payload: bytes


class FrameDecoder:
    """Reassemble frames from a stream of BLE notifications."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        out: list[Frame] = []
        while self._buf:
            start = self._buf.find(bytes((FRAME_START,)))
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 5:
                break
            plen = int.from_bytes(self._buf[3:5], "little")
            total = plen + 10
            if total > 4096:
                del self._buf[0]
                continue
            if len(self._buf) < total:
                break
            pkt = bytes(self._buf[:total])
            if pkt[-1] != FRAME_END or not _integrity_ok(pkt):
                del self._buf[0]
                continue
            out.append(Frame(cmd=pkt[1], seq=pkt[2], payload=pkt[5:-5]))
            del self._buf[:total]
        return out


def _integrity_ok(pkt: bytes) -> bool:
    value = int.from_bytes(pkt[-5:-1], "little")
    if value == 0:
        return True
    return value == (INTEGRITY_SEED + sum(pkt[:-5]) + FRAME_END) & 0xFFFFFFFF


@dataclass(frozen=True)
class Status:
    cmd: int
    voltage_raw: int | None
    bits: int
    density: int
    auto_off: int
    speed: int
    battery: int | None

    @property
    def printing(self) -> bool:
        return bool(self.bits & (1 << 3))

    @property
    def cover_open(self) -> bool:
        return bool(self.bits & (1 << 8))

    @property
    def low_battery(self) -> bool:
        return bool(self.bits & (1 << 9))

    @property
    def overheating(self) -> bool:
        return bool(self.bits & (1 << 10))

    @property
    def paper_out(self) -> bool:
        return bool(self.bits & (1 << 11))

    @property
    def paper_loaded(self) -> bool:
        """Carta dentro (0.5.1, dalla storia di HA: 0x3003 pronta, 0x2903 col coperchio aperto)."""
        return bool(self.bits & (1 << 12))

    @property
    def valid_paper(self) -> bool:
        """Rotolo originale: la stampante ha letto il tag NFC (NTAG213) sul lato del rotolo."""
        return bool(self.bits & (1 << 13))

    @property
    def errors(self) -> list[str]:
        errs = []
        if self.cover_open:
            errs.append("cover_open")
        if self.paper_out:
            errs.append("paper_out")
        if self.overheating:
            errs.append("overheating")
        if self.low_battery:
            errs.append("low_battery")
        return errs


def parse_status(frame: Frame) -> Status | None:
    p = frame.payload
    if frame.cmd not in STATUS_REPLY_COMMANDS:
        return None
    if frame.cmd in (0x80, 0x81) and len(p) != 8:
        return None
    if frame.cmd == 0xFF and len(p) not in (7, 8, 12):
        return None
    return Status(
        cmd=frame.cmd,
        voltage_raw=None if len(p) == 12 else int.from_bytes(p[0:2], "little", signed=True),
        bits=int.from_bytes(p[2:4], "little"),
        density=p[4],
        auto_off=p[5],
        speed=p[6],
        battery=p[7] if len(p) >= 8 else None,
    )


@dataclass(frozen=True)
class PaperDetails:
    """La risposta 0x73 alla domanda 0x72 (TiMini Print, yk_astra_p1): quello che la stampante legge dal tag del
    rotolo originale. Quattro byte; il significato dei numeri è quello di TiMini, da verificare sulla carta vera"""

    paper_type: int
    width: int
    length: int
    color: int
    raw: bytes

    def as_dict(self) -> dict:
        return {"tipo": self.paper_type, "larghezza": self.width, "lunghezza": self.length, "colore": self.color,
                "grezzo": self.raw.hex()}


def parse_paper_details(frame: Frame) -> PaperDetails | None:
    if frame.cmd != Cmd.PAPER_REPLY or len(frame.payload) != 4:
        return None
    p = frame.payload
    return PaperDetails(p[0], p[1], p[2], p[3], bytes(p))


@dataclass
class _Seq:
    value: int = 0

    def take(self) -> int:
        v = self.value
        self.value = (v + 1) % SEQUENCE_MODULUS
        return v


@dataclass
class JobBuilder:
    """Build the frame list for one print job (one or more identical labels)."""

    paper_mode: str = PaperMode.TAG
    density: int = DEFAULT_DENSITY
    speed: int = DEFAULT_SPEED
    start: int = TAG_START
    seq: _Seq = field(default_factory=_Seq)

    def status_query(self) -> bytes:
        return pack_frame(Cmd.STATUS_QUERY, b"", self.seq.take())

    def paper_query(self) -> bytes:
        """«Che carta hai dentro?»: risponde solo con un rotolo originale (bit 13 dello stato)."""
        return pack_frame(Cmd.PAPER_QUERY, b"\x01", self.seq.take())

    def _f(self, cmd: int, payload: bytes = b"") -> bytes:
        return pack_frame(cmd, payload, self.seq.take())

    def _special(self, mode: int, dist: int) -> bytes:
        return self._f(Cmd.FEED_SPECIAL, bytes((mode,)) + dist.to_bytes(2, "little"))

    def _fwd(self, dist: int) -> bytes:
        return self._f(Cmd.FEED_FORWARD, dist.to_bytes(2, "little"))

    def _back(self, dist: int) -> bytes:
        return self._f(Cmd.FEED_BACKWARD, dist.to_bytes(2, "little"))

    def build(self, raster_rows: bytes, copies: int = 1) -> list[bytes]:
        """All copies in one frame list (sequence restarts per page, like the vendor app)."""
        out: list[bytes] = []
        copies = max(1, int(copies))
        for page in range(copies):
            out += self.build_page(raster_rows, first=page == 0, last=page == copies - 1)
        return out

    def build_page(self, raster_rows: bytes, *, first: bool = True, last: bool = True) -> list[bytes]:
        """Frames for one label. raster_rows: packed MSB-first rows of ROW_BYTES each."""
        if len(raster_rows) % ROW_BYTES:
            raise ValueError("raster must be a multiple of the row size")
        self.seq.value = 0
        tag = self.paper_mode in (PaperMode.TAG, PaperMode.BLACK_TAG)
        paper_type = {PaperMode.PLAIN: 0, PaperMode.TAG: 1, PaperMode.BLACK_TAG: 2}[self.paper_mode]
        frames: list[bytes] = []
        if first:
            frames.append(self._f(Cmd.SPEED, bytes((self.speed & 0xFF,))))
            frames.append(self._f(Cmd.DENSITY, bytes((self.density & 0xFF,))))
            frames.append(self._f(Cmd.PAPER_TYPE, bytes((0x01, paper_type))))
            if tag:
                frames.append(self._special(2, 800))
            else:
                frames.append(self._back(12))
        if tag and self.start > 0:
            frames.append(self._fwd(min(120, int(self.start))))
        step = ROW_BYTES * ROWS_PER_SLICE
        for off in range(0, len(raster_rows), step):
            frames.append(self._f(Cmd.RASTER, raster_rows[off : off + step]))
        if tag:
            frames.append(self._special(1 if last else 0, 800))
        else:
            frames.append(self._fwd(52))
            if last:
                frames.append(self._fwd(12))
        return frames

    def feed_label(self) -> list[bytes]:
        """Advance to the next label (tag paper) or feed a short blank strip."""
        if self.paper_mode == PaperMode.PLAIN:
            return [self._fwd(64)]
        paper_type = 2 if self.paper_mode == PaperMode.BLACK_TAG else 1
        return [self._f(Cmd.PAPER_TYPE, bytes((0x01, paper_type))), self._special(1, 800)]


def pack_rows(image, left_padding: int = RASTER_LEFT_PADDING) -> bytes:
    """Pack a portrait PIL image (mode 'L' or '1', dark = ink) into head-width rows."""
    img = image.convert("L")
    w, h = img.size
    if left_padding + w > HEAD_WIDTH:
        raise ValueError(f"image width {w} + padding {left_padding} exceeds head {HEAD_WIDTH}")
    px = img.load()
    out = bytearray()
    for y in range(h):
        row = [0] * HEAD_WIDTH
        for x in range(w):
            if px[x, y] < 128:
                row[left_padding + x] = 1
        for b in range(ROW_BYTES):
            v = 0
            for i in range(8):
                v = (v << 1) | row[b * 8 + i]
            out.append(v)
    return bytes(out)
