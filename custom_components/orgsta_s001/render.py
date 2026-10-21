"""Label layouts for the ORGSTA S001. Pure Pillow, no Home Assistant imports.

Labels are designed in landscape (length x width, text running along the
label) and rotated to the printer's portrait raster at the end.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS = Path(__file__).parent / "assets"
FONT_BOLD = ASSETS / "RobotoCondensed-Bold.ttf"
FONT_REGULAR = ASSETS / "RobotoCondensed-Regular.ttf"
FONT_MDI = ASSETS / "materialdesignicons-webfont.ttf"
MDI_MAP = ASSETS / "mdi_codepoints.json"

SS = 4  # supersampling factor


@dataclass(frozen=True)
class Paper:
    key: str
    label: str
    mode: str  # tag | plain | black_tag
    width_px: int  # across the label (printer head direction)
    length_px: int | None  # along the label; None = continuous

PAPERS: dict[str, Paper] = {
    p.key: p
    for p in (
        Paper("12x40", "12 x 40 mm", "tag", 80, 280),
        Paper("14x40", "14 x 40 mm", "tag", 88, 280),
        Paper("14x30", "14 x 30 mm", "tag", 88, 208),
        Paper("14x22", "14 x 22 mm", "tag", 88, 144),
        Paper("14x50", "14 x 50 mm", "tag", 88, 360),
        Paper("12.5x74", "12,5 x 74 mm", "tag", 84, 544),
        Paper("14x40_trasparente", "14 x 40 mm trasparente", "black_tag", 88, 280),
        Paper("continuo_15", "Continuo 15 mm", "plain", 88, None),
    )
}
DEFAULT_PAPER = "12x40"

LAYOUTS = (
    "testo",
    "icona",
    "qr",
    "dispensa",
    "congelatore",
    "cavo",
    "wifi",
    "data",
    "immagine",
)

LAYOUT_DEFAULT_ICON = {
    "dispensa": "food-variant",
    "congelatore": "snowflake",
    "wifi": "wifi",
    "data": "calendar",
}


class LabelError(ValueError):
    """Raised for bad input (unknown icon, text that cannot fit, ...)."""


@lru_cache(maxsize=1)
def _mdi_map() -> dict[str, int]:
    return json.loads(MDI_MAP.read_text())


def icon_exists(name: str) -> bool:
    return _norm_icon(name) in _mdi_map()


def _norm_icon(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("mdi:"):
        name = name[4:]
    return name


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def parse_date(value, today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    if value is None or value == "" or str(value).lower() in ("oggi", "today"):
        return today
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip().lower()
    if s in ("domani", "tomorrow"):
        return today + dt.timedelta(days=1)
    if s in ("ieri", "yesterday"):
        return today - dt.timedelta(days=1)
    if s.startswith("+") and s[1:].rstrip("dg").isdigit():
        return today + dt.timedelta(days=int(s[1:].rstrip("dg")))
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise LabelError(f"data non valida: {value}")


def fmt_date(d: dt.date, short: bool = False) -> str:
    return d.strftime("%d/%m/%y" if short else "%d/%m/%Y")


@dataclass
class LabelSpec:
    layout: str = "testo"
    title: str = ""
    subtitle: str = ""
    icon: str | None = None
    qr: str | None = None
    date: object = None
    expiry: object = None
    ssid: str | None = None
    password: str | None = None
    image: bytes | None = None
    border: bool = False
    invert: bool = False
    align: str = "center"  # center | left
    paper: str = DEFAULT_PAPER
    length_mm: float | None = None  # continuous paper only
    extra: dict = field(default_factory=dict)


class Canvas:
    """Supersampled landscape canvas in mode L (0 = ink)."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.img = Image.new("L", (w * SS, h * SS), 255)
        self.d = ImageDraw.Draw(self.img)

    # ---- primitives (all coordinates in final pixels) ----
    def text_block(
        self,
        lines: list[tuple[str, str]],
        box: tuple[int, int, int, int],
        *,
        align: str = "center",
        max_size: int | None = None,
        ratios: list[float] | None = None,
    ) -> None:
        """Fit several lines into box. lines = [(text, 'bold'|'regular')]."""
        lines = [(t, s) for t, s in lines if t]
        if not lines:
            return
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        ratios = ratios or [1.0] + [0.62] * (len(lines) - 1)
        ratios = ratios[: len(lines)]
        max_size = max_size or bh
        best = None
        size = max_size
        while size >= 7:
            fonts = [
                _font(str(FONT_BOLD if s == "bold" else FONT_REGULAR), max(6, int(size * r)) * SS)
                for (_, s), r in zip(lines, ratios)
            ]
            metrics = []
            ok = True
            total_h = 0
            for (t, _), f in zip(lines, fonts):
                l, tp, r, b = f.getbbox(t)
                asc, desc = f.getmetrics()
                lh = (asc + desc) * 0.92
                metrics.append((r - l, lh, l))
                total_h += lh
                if (r - l) > bw * SS:
                    ok = False
            if ok and total_h <= bh * SS * 1.04:
                best = (fonts, metrics, total_h)
                break
            size -= 1
        if best is None:
            raise LabelError("il testo non ci sta sull'etichetta, accorcialo o vai a capo")
        fonts, metrics, total_h = best
        y = y0 * SS + (bh * SS - total_h) / 2
        for (t, _), f, (tw, lh, l) in zip(lines, fonts, metrics):
            if align == "left":
                x = x0 * SS - l
            else:
                x = x0 * SS + (bw * SS - tw) / 2 - l
            asc, desc = f.getmetrics()
            self.d.text((x, y + (lh - (asc + desc)) / 2), t, font=f, fill=0)
            y += lh

    def icon(self, name: str, box: tuple[int, int, int, int]) -> None:
        key = _norm_icon(name)
        cp = _mdi_map().get(key)
        if cp is None:
            raise LabelError(f"icona sconosciuta: mdi:{key}")
        x0, y0, x1, y1 = box
        side = min(x1 - x0, y1 - y0)
        f = _font(str(FONT_MDI), int(side * SS * 1.0))
        ch = chr(cp)
        l, t, r, b = f.getbbox(ch)
        cx = (x0 + x1) / 2 * SS
        cy = (y0 + y1) / 2 * SS
        self.d.text((cx - (l + r) / 2, cy - (t + b) / 2), ch, font=f, fill=0)

    def qr(self, data: str, box: tuple[int, int, int, int]) -> None:
        try:
            import qrcode
            from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M
        except ImportError as err:  # pragma: no cover
            raise LabelError("libreria qrcode non disponibile") from err
        x0, y0, x1, y1 = box
        side = min(x1 - x0, y1 - y0)
        for ec in (ERROR_CORRECT_M, ERROR_CORRECT_L):
            q = qrcode.QRCode(border=0, error_correction=ec)
            q.add_data(data)
            q.make(fit=True)
            m = q.get_matrix()
            n = len(m)
            mod = side // n
            if mod >= 2 or (mod >= 1 and ec == ERROR_CORRECT_L):
                break
        if mod < 1:
            raise LabelError("QR troppo lungo per questa etichetta")
        size = mod * n
        ox = x0 + ((x1 - x0) - size) // 2
        oy = y0 + ((y1 - y0) - size) // 2
        for r, row in enumerate(m):
            for c, v in enumerate(row):
                if v:
                    self.d.rectangle(
                        ((ox + c * mod) * SS, (oy + r * mod) * SS, (ox + (c + 1) * mod) * SS - 1, (oy + (r + 1) * mod) * SS - 1),
                        fill=0,
                    )

    def picture(self, data: bytes, box: tuple[int, int, int, int]) -> None:
        try:
            src = Image.open(io.BytesIO(data))
            src.load()
        except Exception as err:  # noqa: BLE001
            raise LabelError("immagine non leggibile") from err
        src = ImageOps.exif_transpose(src)
        if src.mode in ("RGBA", "LA", "P"):
            src = src.convert("RGBA")
            bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
            bg.alpha_composite(src)
            src = bg
        src = src.convert("L")
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        if src.width > src.height and bh > bw or src.height > src.width and bw > bh:
            pass
        src = ImageOps.contain(src, (bw, bh), Image.Resampling.BICUBIC)
        src = ImageOps.autocontrast(src)
        dith = src.convert("1")  # Floyd-Steinberg
        dith = dith.convert("L").resize((dith.width * SS, dith.height * SS), Image.Resampling.NEAREST)
        self.img.paste(dith, ((x0 + (bw - src.width) // 2) * SS, (y0 + (bh - src.height) // 2) * SS))

    def hline_dashed(self, x0: int, x1: int, y: int) -> None:
        for x in range(x0, x1, 6):
            self.d.rectangle((x * SS, y * SS, min(x + 3, x1) * SS - 1, (y + 1) * SS - 1), fill=0)

    def vline_dashed(self, x: int, y0: int, y1: int) -> None:
        for y in range(y0, y1, 6):
            self.d.rectangle((x * SS, y * SS, (x + 1) * SS - 1, min(y + 3, y1) * SS - 1), fill=0)

    def border(self, inset: int = 3, radius: int = 6, width: int = 2) -> None:
        self.d.rounded_rectangle(
            (inset * SS, inset * SS, (self.w - inset) * SS - 1, (self.h - inset) * SS - 1),
            radius=radius * SS,
            outline=0,
            width=width * SS,
        )

    def finish(self, invert: bool = False) -> Image.Image:
        small = self.img.resize((self.w, self.h), Image.Resampling.BICUBIC)
        bw = small.point(lambda v: 0 if v < 150 else 255, mode="L")
        if invert:
            bw = ImageOps.invert(bw)
        return bw


def _length_for(spec: LabelSpec, paper: Paper, natural: int) -> int:
    if paper.length_px:
        return paper.length_px
    if spec.length_mm:
        return max(64, int(round(spec.length_mm * 8)))
    return max(120, natural)


def render(spec: LabelSpec) -> Image.Image:
    """Return the landscape label (mode L, 0 = ink), size (length, width)."""
    paper = PAPERS.get(spec.paper)
    if paper is None:
        raise LabelError(f"carta sconosciuta: {spec.paper}")
    layout = (spec.layout or "testo").lower()
    if layout not in LAYOUTS:
        raise LabelError(f"layout sconosciuto: {layout}")
    H = paper.width_px
    natural = 280
    if paper.length_px is None:
        # continuous paper: estimate a length from the text
        chars = max(len(spec.title or ""), len(spec.subtitle or "")) or 8
        natural = 40 + chars * 26 + (H if (spec.icon or spec.qr or layout in ("qr", "wifi", "icona")) else 0)
    W = _length_for(spec, paper, natural)
    c = Canvas(W, H)
    m = 6 if not spec.border else 9  # margin
    inner = (m, m, W - m, H - m)
    title, sub = (spec.title or "").strip(), (spec.subtitle or "").strip()
    icon = spec.icon or LAYOUT_DEFAULT_ICON.get(layout)

    def lines_of(t: str, s: str) -> list[tuple[str, str]]:
        out = [(ln.strip(), "bold") for ln in t.split("\n") if ln.strip()]
        out += [(ln.strip(), "regular") for ln in s.split("\n") if ln.strip()]
        return out

    def ratios(n_title: int, n_sub: int) -> list[float]:
        return [1.0] * n_title + [0.62] * n_sub

    def with_side(side_draw, text_lines):
        side = H - 2 * m
        side_box = (m, m, m + side, H - m)
        side_draw(side_box)
        tx0 = m + side + 8
        if text_lines:
            nt = sum(1 for _, s in text_lines if s == "bold")
            c.text_block(text_lines, (tx0, m, W - m, H - m), align=spec.align if spec.align else "left", ratios=ratios(nt, len(text_lines) - nt))

    if layout == "testo":
        tl = lines_of(title, sub)
        if not tl:
            raise LabelError("serve un testo")
        if icon and spec.icon:
            with_side(lambda b: c.icon(icon, b), tl)
        else:
            nt = sum(1 for _, s in tl if s == "bold")
            c.text_block(tl, inner, align=spec.align, ratios=ratios(nt, len(tl) - nt))
    elif layout == "icona":
        if not icon:
            raise LabelError("serve un'icona, es. mdi:home")
        tl = lines_of(title, sub)
        if tl:
            with_side(lambda b: c.icon(icon, b), tl)
        else:
            c.icon(icon, inner)
    elif layout == "qr":
        if not spec.qr:
            raise LabelError("serve il contenuto del QR")
        tl = lines_of(title, sub)
        if tl:
            with_side(lambda b: c.qr(spec.qr, (b[0] - 2, b[1] - 3, b[2] + 1, b[3] + 3)), tl)
        else:
            c.qr(spec.qr, inner)
    elif layout in ("dispensa", "congelatore"):
        if not title:
            raise LabelError("serve il nome di cosa c'è dentro")
        d = parse_date(spec.date, spec.extra.get("today"))
        prefix = spec.extra.get("date_prefix") or ("Congelato" if layout == "congelatore" else "")
        when = f"{prefix} {fmt_date(d)}".strip()
        lines = [(title, "bold"), (when, "regular")]
        if spec.expiry not in (None, ""):
            lines.append((f"Scade {fmt_date(parse_date(spec.expiry, spec.extra.get("today")))}", "regular"))
        if sub:
            lines.append((sub, "regular"))
        with_side(lambda b: c.icon(icon, b), lines)
    elif layout == "cavo":
        if not title:
            raise LabelError("serve il nome del cavo")
        half = W // 2
        c.vline_dashed(half, 4, H - 4)
        tl = lines_of(title, sub)
        nt = sum(1 for _, s in tl if s == "bold")
        sub_c = Canvas(half - 2 * m, H - 2 * m)
        if spec.icon:
            side = H - 2 * m
            sub_c.icon(spec.icon, (0, 0, side, side))
            sub_c.text_block(tl, (side + 4, 0, sub_c.w, sub_c.h), ratios=ratios(nt, len(tl) - nt))
        else:
            sub_c.text_block(tl, (0, 0, sub_c.w, sub_c.h), ratios=ratios(nt, len(tl) - nt))
        piece = sub_c.img
        c.img.paste(piece, (m * SS, m * SS))
        c.img.paste(piece.rotate(180), ((half + m) * SS, m * SS))
    elif layout == "wifi":
        if not spec.ssid:
            raise LabelError("serve il nome della rete (ssid)")
        def esc(v: str) -> str:
            return "".join("\\" + ch if ch in '\\;,:"' else ch for ch in v)
        sec = "WPA" if spec.password else "nopass"
        payload = f"WIFI:T:{sec};S:{esc(spec.ssid)};P:{esc(spec.password or '')};;"
        lines = [(spec.ssid, "bold")]
        if spec.password:
            lines.append((spec.password, "regular"))
        with_side(lambda b: c.qr(payload, (b[0] - 2, b[1] - 3, b[2] + 1, b[3] + 3)), lines)
    elif layout == "data":
        d = parse_date(spec.date, spec.extra.get("today"))
        lines = [(fmt_date(d), "bold")]
        if title:
            lines.insert(0, (title, "bold"))
        if sub:
            lines.append((sub, "regular"))
        with_side(lambda b: c.icon(icon, b), lines)
    elif layout == "immagine":
        if not spec.image:
            raise LabelError("serve un'immagine")
        tl = lines_of(title, sub)
        if tl:
            side = H - 2 * m
            c.picture(spec.image, (m, m, m + side, H - m))
            nt = sum(1 for _, s in tl if s == "bold")
            c.text_block(tl, (m + side + 8, m, W - m, H - m), align="left", ratios=ratios(nt, len(tl) - nt))
        else:
            c.picture(spec.image, (2, 2, W - 2, H - 2))

    if spec.border:
        c.border()
    return c.finish(spec.invert)


def to_raster(landscape: Image.Image) -> Image.Image:
    """Rotate the landscape label into the printer's portrait orientation."""
    return landscape.transpose(Image.Transpose.ROTATE_90)


def preview_png(landscape: Image.Image, scale: int = 4) -> bytes:
    """Anteprima com'è sulla carta: i punti della testina (8/mm) fusi come li fonde il calore, non a quadratini.

    Ingrandisce l'immagine a 1 bit con un filtro morbido, la sfuma appena e ripassa una soglia dolce: i bordi
    diventano curve lisce e un punto da solo resta un puntino tondo. Si vede solo, la stampa resta quella.
    """
    w, h = landscape.size
    pad = 2 * scale
    big = landscape.convert("L").resize((w * scale, h * scale), Image.Resampling.BICUBIC)
    big = big.filter(ImageFilter.GaussianBlur(radius=scale * 0.75))
    # soglia dolce attorno a metà: inchiostro quasi nero, carta bianca, 2-3 pixel di bordo sfumato
    ink = big.point(lambda v: 30 + max(0, min(225, (v - 128) * 3 + 112)))
    out = Image.new("RGB", (w * scale + pad * 2, h * scale + pad * 2), (225, 228, 232))
    mask = Image.new("L", out.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, pad + w * scale - 1, pad + h * scale - 1), radius=4 * scale, fill=255)
    label = Image.new("RGB", out.size, (255, 255, 255))
    label.paste(ink.convert("RGB"), (pad, pad))
    out.paste(label, (0, 0), mask)
    # pochi grigi: PNG piccolo anche ingrandito
    out = out.quantize(colors=16, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
