"""Label layouts for the ORGSTA S001. Pure Pillow, no Home Assistant imports.

Labels are designed in landscape (length x width, text running along the
label) and rotated to the printer's portrait raster at the end.
"""

from __future__ import annotations

import math
import re

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

# 0.6.0 (7 novembre, Morgan: «ogni tanto mi stampa un foglio vuoto dopo quello che stampa», col rotolo piccolo):
# la 14x30 stampava 208 righe su 240 e ne restavano 12 dopo la stampa, contro le 16 della 12x40 che non l'ha mai fatto;
# se la stampante si fermava un po' più avanti nel buco la stampa finiva nell'etichetta dopo e «vai al buco» la buttava
# tutta. Ora ogni carta lascia 40 righe (5 mm) come la 12x40: 14x30 → 200, 14x22 → 136
PAPERS: dict[str, Paper] = {
    p.key: p
    for p in (
        Paper("12x40", "12 x 40 mm", "tag", 80, 280),
        Paper("14x40", "14 x 40 mm", "tag", 88, 280),
        Paper("14x30", "14 x 30 mm", "tag", 88, 200),
        Paper("14x22", "14 x 22 mm", "tag", 88, 136),
        Paper("14x50", "14 x 50 mm", "tag", 88, 360),
        Paper("12.5x74", "12,5 x 74 mm", "tag", 84, 544),
        Paper("14x40_trasparente", "14 x 40 mm trasparente", "black_tag", 88, 280),
        Paper("continuo_15", "Continuo 15 mm", "plain", 88, None),
    )
}
DEFAULT_PAPER = "12x40"


def tag_start(paper: Paper) -> int:
    """Quanto avanzare dopo il buco prima di stampare, per carta (0.5.3). 24 centra la 12x40 (320 righe di etichetta,
    280 stampate, misurato il 21 ottobre): stesso conto per le altre, metà della differenza col margine della 12x40"""
    from .protocol import TAG_START
    m = re.match(r"^(\d+(?:\.\d+)?)x(\d+)", paper.key)
    if not m or paper.length_px is None:
        return TAG_START
    margine = int(m.group(2)) * 8 - paper.length_px
    return max(0, TAG_START + (margine - 40) // 2)

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
    length_mm: float | None = None  # continuous paper only (the cable: the whole flag)
    # 0.8.0 (11 novembre, rotolo continuo): la lunghezza giusta per il testo a scatti di `length_step` mm, dentro
    # `length_min`/`length_max` (per il cavo: per lato); il cavo a bandiera col diametro esterno; il segno NFC
    length_step: float | None = None
    length_min: float | None = None
    length_max: float | None = None
    diameter_mm: float | None = None
    nfc: bool = False
    extra: dict = field(default_factory=dict)


MIN_PX = 9  # sotto (in punti della stampante, 8 al mm) una riga non si legge più


def _misura(t: str, bold: bool, size: int) -> tuple[float, float]:
    """(larghezza, altezza della riga) in punti della stampante"""
    f = _font(str(FONT_BOLD if bold else FONT_REGULAR), max(1, size) * SS)
    l, _tp, r, _b = f.getbbox(t)
    asc, desc = f.getmetrics()
    return (r - l) / SS, (asc + desc) * 0.92 / SS


def _cap(t: str, bold: bool, bw: float, top: int) -> int:
    """la grandezza più grande che sta nella larghezza (al massimo top)"""
    lo, hi = 1, max(1, top)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _misura(t, bold, mid)[0] <= bw:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _adatta(lines, ratios, bw, bh, top, gruppi=None):
    """Le grandezze delle righe: la base più grande che ci sta in altezza, ogni riga fermata dalla sua larghezza e le
    normali mai sopra il titolo. Ritorna (punteggio, righe, grandezze) o None. Il punteggio è la riga più piccola
    rispetto a quanto vorrebbe essere: vince l'impaginazione dove nessuna riga resta schiacciata"""
    caps = [_cap(t, st == "bold", bw, int(top)) for t, st in lines]
    for g in set(gruppi or []) - {None}:                   # le due metà di un titolo hanno la stessa grandezza
        dentro = [i for i, x in enumerate(gruppi) if x == g]
        caps = [min(caps[j] for j in dentro) if i in dentro else c for i, c in enumerate(caps)]
    def grandezze(base: int) -> list[int]:
        out = [min(caps[i], max(1, int(base * r))) for i, r in enumerate(ratios)]
        bold = [sz for sz, (_, st) in zip(out, lines) if st == "bold"]
        if bold:
            tetto = int(min(bold) * 0.92)
            out = [sz if st == "bold" else min(sz, max(tetto, 1)) for sz, (_, st) in zip(out, lines)]
        return out
    for base in range(int(top), 0, -1):
        sizes = grandezze(base)
        h = sum(_misura(t, st == "bold", sz)[1] for (t, st), sz in zip(lines, sizes))
        if h <= bh * 1.04:
            if min(sizes) < MIN_PX:
                return None
            return min(sz / r for sz, r in zip(sizes, ratios)), lines, sizes
    return None


def _varianti(lines, ratios):
    """com'è, e col titolo lungo diviso in due righe nel punto più vicino a metà"""
    yield lines, ratios, None
    for i, (t, st) in enumerate(lines):
        if st != "bold" or " " not in t.strip():
            continue
        parole = t.split()
        tagli = [(abs(len(" ".join(parole[:k])) - len(" ".join(parole[k:]))), k) for k in range(1, len(parole))]
        _, k = min(tagli)
        nuove = lines[:i] + [(" ".join(parole[:k]), st), (" ".join(parole[k:]), st)] + lines[i + 1:]
        gruppi = [None] * i + [0, 0] + [None] * (len(lines) - i - 1)
        yield nuove, ratios[:i] + [ratios[i], ratios[i]] + ratios[i + 1:], gruppi
        break


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
        """Fit several lines into box. lines = [(text, 'bold'|'regular')].

        0.6.0 (Morgan, 7 novembre: «le scritte sotto non si vedono, allarga un po' tanto c'è spazio»): ogni riga prende
        la grandezza che le lascia la sua larghezza, senza dipendere dalle altre (prima tutte scendevano col titolo
        lungo); le righe normali mai più grandi del titolo; un titolo lungo va su due righe se così viene più grande;
        sotto MIN_PX una riga non si legge e l'etichetta non si fa"""
        lines = [(t, s) for t, s in lines if t]
        if not lines:
            return
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        ratios = list(ratios or [1.0] + [0.62] * (len(lines) - 1))
        ratios = (ratios + [0.62] * len(lines))[: len(lines)]
        best = None
        for ls, rs, gr in _varianti(lines, ratios):
            fit = _adatta(ls, rs, bw, bh, max_size or bh, gr)
            # il titolo diviso in due solo se viene davvero più grande (10%)
            if fit is not None and (best is None or fit[0] > best[0] * (1.1 if gr else 1) + 0.01):
                best = fit
        if best is None:
            raise LabelError("il testo non ci sta sull'etichetta, accorcialo o togli una riga")
        _, ls, sizes = best
        rows = []
        total_h = 0.0
        for (t, st), sz in zip(ls, sizes):
            f = _font(str(FONT_BOLD if st == "bold" else FONT_REGULAR), sz * SS)
            l, _tp, r, _b = f.getbbox(t)
            asc, desc = f.getmetrics()
            lh = (asc + desc) * 0.92
            rows.append((t, f, r - l, lh, l))
            total_h += lh
        y = y0 * SS + (bh * SS - total_h) / 2
        for t, f, tw, lh, l in rows:
            x = x0 * SS - l if align == "left" else x0 * SS + (bw * SS - tw) / 2 - l
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
    return _a_scatti(spec, natural, 120)


def _a_scatti(spec: LabelSpec, natural: float, minimo: int) -> int:
    """La lunghezza naturale (punti) arrotondata per eccesso allo scatto e dentro minimo e massimo (0.8.0)"""
    n = float(natural)
    if spec.length_step:
        passo = float(spec.length_step) * 8
        n = math.ceil(n / passo - 1e-6) * passo
    if spec.length_min:
        n = max(n, float(spec.length_min) * 8)
    if spec.length_max:
        n = min(n, float(spec.length_max) * 8)
    return max(minimo, int(round(n)))


def larghezza_naturale(lines: list[tuple[str, str]], ratios: list[float], bh: int) -> float:
    """Quanto è lungo il testo scritto più grande che sta nell'altezza (0.8.0: sul continuo la lunghezza la decide il
    testo vero, non più «26 punti a lettera»)"""
    lines = [(t, st) for t, st in lines if t]
    if not lines:
        return 0.0
    ratios = (list(ratios) + [0.62] * len(lines))[: len(lines)]
    fit = _adatta(lines, ratios, 100000, bh, bh)
    if fit is None:
        return 0.0
    _, ls, sizes = fit
    return max(_misura(t, st == "bold", sz)[0] for (t, st), sz in zip(ls, sizes))


def giro_px(diametro_mm: float) -> int:
    """Il pezzo che si avvolge intorno al cavo: la circonferenza più 2 mm (0.8.0)"""
    return int(round((math.pi * float(diametro_mm) + 2.0) * 8))


def render(spec: LabelSpec) -> Image.Image:
    """Return the landscape label (mode L, 0 = ink), size (length, width)."""
    paper = PAPERS.get(spec.paper)
    if paper is None:
        raise LabelError(f"carta sconosciuta: {spec.paper}")
    layout = (spec.layout or "testo").lower()
    if layout not in LAYOUTS:
        raise LabelError(f"layout sconosciuto: {layout}")
    H = paper.width_px
    m = 6 if not spec.border else 9  # margin
    title, sub = (spec.title or "").strip(), (spec.subtitle or "").strip()
    icon = spec.icon or LAYOUT_DEFAULT_ICON.get(layout)

    def lines_of(t: str, s: str) -> list[tuple[str, str]]:
        out = [(ln.strip(), "bold") for ln in t.split("\n") if ln.strip()]
        out += [(ln.strip(), "regular") for ln in s.split("\n") if ln.strip()]
        return out

    def ratios(n_title: int, n_sub: int) -> list[float]:
        return [1.0] * n_title + [0.62] * n_sub

    nfc_w = int(round((H - 2 * m) * 0.62)) + 6 if spec.nfc else 0
    if layout == "cavo" and spec.diameter_mm:
        return _cavo_bandiera(spec, paper, H, m, lines_of(title, sub), ratios, nfc_w)
    if layout == "cavo":
        nfc_w = 0                            # le due metà di prima: il segno NFC non ha posto
    natural = 280
    if paper.length_px is None:
        natural = _naturale(spec, layout, H, m, lines_of, ratios, icon) + nfc_w
    W = _length_for(spec, paper, natural)
    c = Canvas(W, H)
    R = W - m - nfc_w                        # dove finisce il contenuto: dopo c'è il segno NFC
    inner = (m, m, R, H - m)

    def with_side(side_draw, text_lines):
        side = H - 2 * m
        side_box = (m, m, m + side, H - m)
        side_draw(side_box)
        tx0 = m + side + 8
        if text_lines:
            nt = sum(1 for _, s in text_lines if s == "bold")
            c.text_block(text_lines, (tx0, m, R, H - m), align=spec.align if spec.align else "left", ratios=ratios(nt, len(text_lines) - nt))

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
        half = W // 2                      # senza diametro: le due metà di prima (carta a etichette)
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
            c.text_block(tl, (m + side + 8, m, R, H - m), align="left", ratios=ratios(nt, len(tl) - nt))
        else:
            c.picture(spec.image, (2, 2, R + m - 2, H - 2))

    if nfc_w:
        _segno_nfc(c, W - m - nfc_w + 6, m, W - m, H - m)
    if spec.border:
        c.border()
    return c.finish(spec.invert)


def _naturale(spec: LabelSpec, layout: str, H: int, m: int, lines_of, ratios, icon) -> float:
    """La lunghezza naturale sul continuo (punti), dal testo vero (0.8.0)"""
    bh = H - 2 * m
    side = bh + 8                                    # il quadrato dell'icona o del QR e lo spazio dopo
    title, sub = (spec.title or "").strip(), (spec.subtitle or "").strip()

    def testo(lines) -> float:
        nt = sum(1 for _, s in lines if s == "bold")
        return larghezza_naturale(lines, ratios(nt, len(lines) - nt), bh)

    tl = lines_of(title, sub)
    if layout == "testo":
        return 2 * m + testo(tl) + (side if spec.icon else 0) + 4
    if layout in ("icona", "qr"):
        return 2 * m + (side + testo(tl) + 4 if tl else bh)
    if layout in ("dispensa", "congelatore"):
        d = parse_date(spec.date, spec.extra.get("today"))
        prefix = spec.extra.get("date_prefix") or ("Congelato" if layout == "congelatore" else "")
        righe = [(title, "bold"), (f"{prefix} {fmt_date(d)}".strip(), "regular")]
        if spec.expiry not in (None, ""):
            righe.append((f"Scade {fmt_date(parse_date(spec.expiry, spec.extra.get('today')))}", "regular"))
        if sub:
            righe.append((sub, "regular"))
        return 2 * m + side + testo(righe) + 4
    if layout == "wifi":
        righe = [(spec.ssid or "", "bold")] + ([(spec.password, "regular")] if spec.password else [])
        return 2 * m + side + testo(righe) + 4
    if layout == "data":
        righe = ([(title, "bold")] if title else []) + [(fmt_date(parse_date(spec.date, spec.extra.get("today"))),
                                                          "bold")] + ([(sub, "regular")] if sub else [])
        return 2 * m + side + testo(righe) + 4
    if layout == "immagine":
        if tl:
            return 2 * m + side + testo(tl) + 4
        if spec.image:
            try:
                im = Image.open(io.BytesIO(spec.image))
                return 4 + (H - 4) * im.width / max(1, im.height)
            except Exception:  # noqa: BLE001 - il disegno dirà che non si legge
                pass
        return 280
    if layout == "cavo":
        return 2 * (2 * m + testo(tl) + (side if spec.icon else 0) + 4)
    return 280


def _segno_nfc(c: "Canvas", x0: int, y0: int, x1: int, y1: int) -> None:
    """Il segno NFC in fondo: qui c'è un tag da avvicinare al telefono (0.8.0)"""
    c.icon("nfc-variant", (x0, y0, x1, y1))


def _cavo_lato(spec: LabelSpec, w: int, h: int, tl, ratios, nfc_w: int) -> Image.Image:
    """Un lato della bandiera: QR (se c'è), icona (se chiesta), testo, segno NFC. Supersampled, 0 = inchiostro"""
    sub_c = Canvas(w, h)
    x = 0
    if spec.qr:
        sub_c.qr(spec.qr, (x - 2, -3, x + h + 1, h + 3))
        x += h + 6
    elif spec.icon:
        sub_c.icon(spec.icon, (x, 0, x + h, h))
        x += h + 4
    fine = w - nfc_w
    if tl:
        nt = sum(1 for _, s in tl if s == "bold")
        sub_c.text_block(tl, (x, 0, fine, h), align="left" if x else "center", ratios=ratios(nt, len(tl) - nt))
    if nfc_w:
        _segno_nfc(sub_c, w - nfc_w + 6, 0, w, h)
    return sub_c.img


def _cavo_bandiera(spec: LabelSpec, paper: Paper, H: int, m: int, tl, ratios, nfc_w: int) -> Image.Image:
    """Il cavo a bandiera (0.8.0, Morgan: «ogni cavo avrà la dimensione del diametro esterno, lascia lo spazio in mezzo
    e scrive sui due lati, qr e tutto il resto»): lato A · giro del cavo · lato B capovolto. Si avvolge il giro e si
    incollano A e B schiena contro schiena: da tutti e due i lati si legge dritto"""
    if not tl and not spec.qr:
        raise LabelError("serve il nome del cavo")
    giro = giro_px(spec.diameter_mm)
    bh = H - 2 * m
    if paper.length_px:
        lato = (paper.length_px - giro) // 2
        if lato < bh + 2 * m:
            raise LabelError("il cavo è troppo grosso per questa etichetta: serve il rotolo continuo")
    elif spec.length_mm:
        lato = max(int(round((float(spec.length_mm) * 8 - giro) / 2)), 2 * m + bh // 2)
    else:
        nt = sum(1 for _, s in tl if s == "bold")
        nat = 2 * m + larghezza_naturale(tl, ratios(nt, len(tl) - nt), bh) + 4
        if spec.qr:
            nat += bh + 6
        elif spec.icon:
            nat += bh + 4
        lato = _a_scatti(spec, nat + nfc_w, 2 * m + bh)
    W = 2 * lato + giro
    c = Canvas(W, H)
    pezzo = _cavo_lato(spec, lato - 2 * m, bh, tl, ratios, nfc_w)
    c.img.paste(pezzo, (m * SS, m * SS))
    c.img.paste(pezzo.rotate(180), ((lato + giro + m) * SS, m * SS))
    c.vline_dashed(lato, 4, H - 4)
    c.vline_dashed(lato + giro, 4, H - 4)
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
