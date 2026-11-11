"""State holder and print queue for one S001 printer."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DEFAULT_DENSITY, DOMAIN
from .ponte import Pagina, Ponte, Proxy, proxy_con_coda
from .printer import PrinterError, S001Session, check_ready
from .protocol import DEFAULT_SPEED, ROW_BYTES, TAG_START, JobBuilder, PaperDetails, Status, pack_rows
from .render import DEFAULT_PAPER, PAPERS, LabelSpec, preview_png, render, tag_start, to_raster

_LOGGER = logging.getLogger(__name__)


@dataclass
class PrinterState:
    status: Status | None = None
    status_time: datetime | None = None
    last_print: datetime | None = None
    last_text: str | None = None
    # 0.8.1 (11 novembre, «stampa a pezzi»): da dove è passata l'ultima stampa e quanto ci ha messo, per le prove
    last_via: dict = field(default_factory=dict)
    last_error: str | None = None
    busy: bool = False
    labels_printed: int = 0
    preview: bytes | None = None
    preview_time: datetime | None = None
    reachable: bool | None = None
    powered: bool | None = None
    seen_time: datetime | None = None
    # dove sta: il proxy Bluetooth che la sente meglio e la sua area (la stampantina è tascabile, si sposta)
    proxy: str | None = None
    proxy_address: str | None = None
    area: str | None = None
    area_id: str | None = None
    rssi: int | None = None
    extra: dict = field(default_factory=dict)
    roll_used: int = 0
    roll_capacity: int | None = None
    roll_counted: bool = False
    roll_changed: datetime | None = None
    # i rotoli (0.5.0): il colore di quello dentro, i nuovi nel cassetto per colore, quelli tolti a metà con le
    # etichette che gli restano, se è finito, e se la stampante dice che forse è stato cambiato (coperchio aperto,
    # carta finita e poi di nuovo carta) e nessuno l'ha ancora detto
    roll_color: str = "bianco"
    roll_finished: bool = False
    stock: dict = field(default_factory=dict)
    started: dict = field(default_factory=dict)
    roll_check: str | None = None
    roll_check_time: datetime | None = None
    # il tag NFC dei rotoli originali (0.5.1): originale = l'ultima volta che la stampante l'ha detto (bit 13), tag =
    # i 4 byte che risponde a «che carta hai dentro?»; la carta (12x40, 14x30…) di ogni colore, da «Cambia rotolo»
    original: bool | None = None
    tag: dict | None = None
    tag_time: datetime | None = None
    roll_papers: dict = field(default_factory=dict)
    roll_mm: float = 0.0                          # 0.8.0: i millimetri usati del rotolo dentro (il continuo)

    @property
    def roll_left(self) -> int | None:
        if self.roll_color == CONTINUOUS_COLOR or not self.roll_counted or not self.roll_capacity:
            return None
        return max(0, self.roll_capacity - self.roll_used)


# Sotto queste etichette un rotolo «finito» non insegna niente (carta tolta a metà, sensore che sbaglia)
ROLL_MIN_LEARN = 10
# i colori dei rotoli (il nome è anche l'opzione del menu «Rotolo montato»)
ROLL_COLORS = {"bianco": "#ffffff", "azzurro": "#7fd3ff", "giallo": "#ffe14d", "rosa": "#ffaecf",
               "continuo": "#ffffff"}
# 0.8.0 (11 novembre, Morgan: «i rotoli ora sono tutti bianchi [...] ora i rotoli bianchi sono infiniti»): il rotolo
# continuo da 15 mm, senza buchi. Non si conta (niente capienza, niente rimaste): solo i centimetri usati
CONTINUOUS_COLOR = "continuo"
NEW_PAPERS = {"continuo": "continuo_15"}
ROLL_KINDS = ("new", "started", "same", "unknown")
# la carta dei rotoli nuovi comprati da Morgan (colorati e bianchi, 25 ottobre 2026): 14 x 30 mm, 210 etichette
NEW_ROLL_PAPER = "14x30"
# i rotoli originali ORGSTA (col tag NFC) di Morgan: bianchi 12 x 40 mm
ORIGINAL_PAPER = "12x40"
ORIGINAL_COLOR = "bianco"
# le letture dello stato dopo l'accensione (secondi): il coperchio aperto e richiuso si vede anche senza stampare
AUTO_READS = (4, 30, 90)


type S001ConfigEntry = ConfigEntry["S001Coordinator"]


class S001Coordinator(DataUpdateCoordinator[PrinterState]):
    """Holds the last known state; talks to the printer only when asked."""

    config_entry: S001ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: S001ConfigEntry, address: str) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{address}", update_interval=None)
        self.address = address
        self._lock = asyncio.Lock()
        self.density: int = DEFAULT_DENSITY
        self.offset: int = 0                      # «Spostamento stampa», in punti (8 = 1 mm), sulla carta a etichette
        self.paper: str = DEFAULT_PAPER
        self.quick_text: str = ""
        self.paper_saved: set[str] = set()       # le chiavi già nel registro (la carta c'è dalla 0.5.1)
        self.data = PrinterState()
        self._auto_task: asyncio.Task | None = None
        # fino a quando un proxy resta collegato alla stampantina dopo una stampa (0.7.0): intanto niente
        # collegamenti diretti da HA, la stampantina ne accetta uno solo
        self._ponte_fino = 0.0
        # 0.9.4 (Morgan, 11 novembre: «non possiamo comandare noi quale proxy la prende quindi forziamola li»): un proxy
        # fisso per la coda. Con lui niente altri proxy, niente stampa in diretta e niente letture in diretta di HA
        self.proxy_fisso: str = ""
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{address.replace(':', '').lower()}")

    # ---- rotolo: etichette usate da quando è stato messo, capienza imparata quando finisce ----
    async def async_load_roll(self) -> None:
        data = await self._store.async_load() or {}
        self.paper_saved = set(data)
        d = self.data
        d.roll_used = int(data.get("used") or 0)
        d.roll_mm = float(data.get("mm") or 0)
        cap = data.get("capacity")
        d.roll_capacity = int(cap) if cap else None
        d.roll_counted = bool(data.get("counted"))
        d.roll_changed = dt_util.parse_datetime(data["changed"]) if data.get("changed") else None
        d.roll_color = data.get("color") if data.get("color") in ROLL_COLORS else "bianco"
        d.roll_finished = bool(data.get("finished"))
        d.stock = {c: max(0, int((data.get("stock") or {}).get(c) or 0)) for c in ROLL_COLORS}
        d.started = {c: [int(x) for x in (data.get("started") or {}).get(c) or [] if int(x) > 0] for c in ROLL_COLORS}
        d.roll_check = data.get("check")
        d.roll_check_time = dt_util.parse_datetime(data["check_time"]) if data.get("check_time") else None
        d.original = data.get("original")
        d.tag = data.get("tag")
        d.tag_time = dt_util.parse_datetime(data["tag_time"]) if data.get("tag_time") else None
        d.roll_papers = {c: p for c, p in (data.get("papers") or {}).items() if c in ROLL_COLORS and p in PAPERS}
        if data.get("paper") in PAPERS:
            self.paper = data["paper"]

    @callback
    def _save_roll(self) -> None:
        d = self.data
        self._store.async_delay_save(
            lambda: {
                "used": d.roll_used,
                "mm": round(d.roll_mm, 1),
                "capacity": d.roll_capacity,
                "counted": d.roll_counted,
                "changed": d.roll_changed.isoformat() if d.roll_changed else None,
                "color": d.roll_color,
                "finished": d.roll_finished,
                "stock": d.stock,
                "started": d.started,
                "check": d.roll_check,
                "check_time": d.roll_check_time.isoformat() if d.roll_check_time else None,
                "original": d.original,
                "tag": d.tag,
                "tag_time": d.tag_time.isoformat() if d.tag_time else None,
                "papers": d.roll_papers,
                "paper": self.paper,
            },
            1,
        )

    @callback
    def async_new_roll(self) -> None:
        """«Rotolo nuovo»: un rotolo nuovo dello stesso colore di quello dentro (dal cassetto)."""
        self.async_change_roll(self.data.roll_color, "new")

    @callback
    def async_change_roll(self, color: str, kind: str, paper: str | None = None, used: int | None = None) -> dict:
        """Il rotolo cambiato: `new` uno nuovo dal cassetto (si conta da zero), `started` uno tolto a metà rimesso
        (riparte dalle etichette che gli restavano), `unknown` uno iniziato di cui non si sa niente, `same` è ancora
        quello di prima (si toglie solo il «da confermare"). Quello che c'era, se non era finito e si sapeva quante
        ne restavano, va tra gli iniziati del suo colore. La carta: quella detta, se no per un rotolo nuovo 14x30
        (quelli comprati), se no quella che aveva quel colore l'ultima volta; resta quella di prima se non si sa."""
        if paper is not None and paper not in PAPERS:
            raise ValueError(f"carta sconosciuta: {paper}")
        if color not in ROLL_COLORS:
            raise ValueError(f"colore sconosciuto: {color}")
        if kind not in ROLL_KINDS:
            raise ValueError(f"cambio sconosciuto: {kind}")
        d = self.data
        if kind != "same":
            left = d.roll_left
            if not d.roll_finished and left and d.roll_color != CONTINUOUS_COLOR:
                d.started.setdefault(d.roll_color, []).append(int(left))
            if kind == "new":
                d.stock[color] = max(0, int(d.stock.get(color) or 0) - 1)
                d.roll_used, d.roll_counted = 0, True
            if color == CONTINUOUS_COLOR:
                d.roll_counted = False                    # infinito: non si conta
            elif kind == "started" and d.started.get(color):
                left = d.started[color].pop()
                d.roll_counted = bool(d.roll_capacity)
                d.roll_used = max(0, (d.roll_capacity or left) - left)
            else:
                d.roll_used, d.roll_counted = 0, False
            d.roll_color, d.roll_finished = color, False
            d.roll_changed = dt_util.utcnow()
            d.roll_mm = 0.0
            paper = paper or (NEW_PAPERS.get(color, NEW_ROLL_PAPER) if kind == "new"
                              else d.roll_papers.get(color) or NEW_PAPERS.get(color))
        else:
            d.roll_finished = False                       # «è lo stesso»: allora non era finito
        if paper:
            self.paper = paper
            d.roll_papers[color] = paper
        if used is not None:                              # 0.5.4: le etichette già uscite, corrette a mano
            d.roll_used = max(0, int(used))
        d.roll_check, d.roll_check_time = None, None
        self._save_roll()
        self.async_set_updated_data(d)
        return self.roll_info()

    @callback
    def async_set_stock(self, color: str, n: int) -> None:
        self.data.stock[color] = max(0, int(n))
        self._save_roll()
        self.async_set_updated_data(self.data)

    @callback
    def async_set_color(self, color: str) -> None:
        """Il colore di quello dentro corretto a mano (senza toccare conti e cassetto)."""
        if color in ROLL_COLORS:
            self.data.roll_color = color
            self._save_roll()
            self.async_set_updated_data(self.data)

    def roll_info(self) -> dict:
        d = self.data
        return {"colore": d.roll_color, "usate": d.roll_used, "capienza": d.roll_capacity, "rimaste": d.roll_left,
                "contato_da_nuovo": d.roll_counted, "finito": d.roll_finished,
                "scorte": {c: int(d.stock.get(c) or 0) for c in ROLL_COLORS},
                "iniziati": {c: list(d.started.get(c) or []) for c in ROLL_COLORS},
                "colori": ROLL_COLORS, "da_confermare": d.roll_check,
                "da_confermare_dal": d.roll_check_time.isoformat() if d.roll_check_time else None,
                "carta": self.paper, "carte": dict(d.roll_papers), "originale": d.original, "tag": d.tag,
                "infinito": self.continuous, "mm_usati": round(d.roll_mm, 1)}

    @property
    def continuous(self) -> bool:
        """Dentro c'è carta senza buchi (il rotolo continuo, 0.8.0): niente conti delle etichette"""
        p = PAPERS.get(self.paper)
        return bool(p and p.length_px is None)

    @callback
    def _roll_maybe_changed(self, why: str) -> None:
        d = self.data
        if d.roll_check is None or why == "carta":
            d.roll_check, d.roll_check_time = why, dt_util.utcnow()
            _LOGGER.debug("S001 %s: rotolo forse cambiato (%s)", self.address, why)
            self._save_roll()

    @callback
    def async_set_paper(self, paper: str) -> None:
        """«Carta caricata» scelta a mano: vale anche per il colore dentro."""
        if paper in PAPERS:
            self.paper = paper
            self.data.roll_papers[self.data.roll_color] = paper
            self._save_roll()
            self.async_set_updated_data(self.data)

    @callback
    def _original_changed(self, original: bool) -> None:
        """Il tag è comparso o sparito (0.5.3): col tag è il rotolo originale (bianco, 12x40, quante ne restano non
        si sa) e non serve chiedere niente; senza tag è uno dei nuovi (14x30) e il colore lo dice qualcuno"""
        d = self.data
        if original:
            _LOGGER.info("S001 %s: rimesso il rotolo originale", self.address)
            self.async_change_roll(ORIGINAL_COLOR, "unknown", ORIGINAL_PAPER)
            return
        # 0.8.0: i rotoli di adesso sono quelli continui: si mette quello, e si chiede lo stesso (roll_check)
        _LOGGER.info("S001 %s: tolto il rotolo originale, dentro probabilmente il continuo", self.address)
        self.paper = d.roll_papers.get(CONTINUOUS_COLOR) or NEW_PAPERS[CONTINUOUS_COLOR]
        d.roll_color = CONTINUOUS_COLOR
        d.roll_counted, d.roll_used = False, 0
        self._roll_maybe_changed("tag")
        self._save_roll()

    @callback
    def _on_paper(self, pd: PaperDetails | None) -> None:
        """Il tag letto: se è diverso dall'ultimo, il rotolo dentro è un altro."""
        if pd is None:
            return
        d = self.data
        tag = pd.as_dict()
        if d.tag is not None and d.tag.get("grezzo") != tag["grezzo"]:
            self._roll_maybe_changed("tag")
        if tag != d.tag:
            _LOGGER.info("S001 %s: tag del rotolo %s", self.address, tag)
        d.tag, d.tag_time = tag, dt_util.utcnow()
        carta = f"{pd.width}x{pd.length}" if pd.length else f"continuo_{pd.width}"
        if carta in PAPERS and self.paper != carta:            # la misura scritta nel tag vince
            self.paper = carta
            if PAPERS[carta].length_px is None:                # un continuo originale: è il rotolo continuo
                d.roll_color, d.roll_counted = CONTINUOUS_COLOR, False
            d.roll_papers[d.roll_color] = carta
        self._save_roll()

    async def _read_tag(self, session: S001Session, builder: JobBuilder) -> None:
        try:
            self._on_paper(await session.query_paper(builder))
        except Exception:  # noqa: BLE001 - il tag è un di più: mai che fermi una stampa
            _LOGGER.debug("lettura del tag non riuscita", exc_info=True)

    @callback
    def async_set_roll_capacity(self, value: int | None) -> None:
        self.data.roll_capacity = int(value) if value else None
        self._save_roll()
        self.async_set_updated_data(self.data)

    @callback
    def _roll_paper_out(self) -> None:
        """Carta finita su un rotolo contato dall'inizio: quante ne ha fatte è la capienza."""
        d = self.data
        if d.roll_counted and d.roll_used > ROLL_MIN_LEARN and not self.continuous:
            _LOGGER.info("S001 %s: rotolo finito dopo %s etichette, capienza imparata", self.address, d.roll_used)
            d.roll_capacity = d.roll_used
        d.roll_finished = True
        d.roll_counted = False
        self._save_roll()

    # ---- accesa/spenta dagli annunci Bluetooth (i proxy li sentono già, nessuna connessione) ----
    @callback
    def async_start(self) -> None:
        entry = self.config_entry
        entry.async_on_unload(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advert,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=False),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        entry.async_on_unload(
            bluetooth.async_track_unavailable(self.hass, self._on_gone, self.address, connectable=False)
        )
        if bluetooth.async_address_present(self.hass, self.address, connectable=False):
            self.data.powered = True
            self.data.seen_time = dt_util.utcnow()
            if info := bluetooth.async_last_service_info(self.hass, self.address, connectable=False):
                self.data.rssi = info.rssi
                self._locate(info.source)

    @callback
    def _on_advert(self, info, change) -> None:
        was = self.data.powered
        self.data.powered = True
        self.data.seen_time = dt_util.utcnow()
        self.data.rssi = getattr(info, "rssi", None)
        moved = self._locate(getattr(info, "source", None))
        if moved and was is True:
            self.async_set_updated_data(self.data)
        if was is not True:
            _LOGGER.debug("S001 %s accesa", self.address)
            self.async_set_updated_data(self.data)
            self._schedule_auto_refresh()

    @callback
    def _locate(self, source: str | None) -> bool:
        """Proxy e area dal proxy che la sente meglio (l'area del dispositivo del proxy). True se è cambiato."""
        if not source or source == self.data.proxy_address:
            return False
        name, area_id = source, None
        scanner = bluetooth.async_scanner_by_source(self.hass, source)
        if scanner is not None and getattr(scanner, "name", None):
            name = scanner.name
        devreg = dr.async_get(self.hass)
        dev = None
        for kind in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC):
            dev = devreg.async_get_device(connections={(kind, source.lower())}) or devreg.async_get_device(
                connections={(kind, source.upper())}
            )
            if dev:
                break
        if dev is not None:
            name = dev.name_by_user or dev.name or name
            area_id = dev.area_id
        area = None
        if area_id and (a := ar.async_get(self.hass).async_get_area(area_id)):
            area = a.name
        d = self.data
        changed = (name, area_id) != (d.proxy, d.area_id)
        d.proxy_address, d.proxy, d.area_id, d.area = source, name, area_id, area
        if changed:
            _LOGGER.debug("S001 %s vicina a %s (%s)", self.address, name, area)
        return changed

    @callback
    def _on_gone(self, info) -> None:
        if self.data.busy:
            return
        self.data.powered = False
        _LOGGER.debug("S001 %s spenta", self.address)
        self.async_set_updated_data(self.data)

    @callback
    def _schedule_auto_refresh(self) -> None:
        if self._auto_task and not self._auto_task.done():
            return
        self._auto_task = self.config_entry.async_create_background_task(
            self.hass, self._auto_refresh(), f"{DOMAIN}_auto_refresh"
        )

    async def _auto_refresh(self) -> None:
        """Qualche lettura dopo l'accensione (AUTO_READS): batteria, coperchio, carta, tag. Chi cambia il rotolo di
        solito la accende, apre, cambia e chiude: così il cambio si vede anche senza stampare."""
        fatto = 0
        for quando in AUTO_READS:
            await asyncio.sleep(max(0, quando - fatto))
            fatto = quando
            if not self.data.powered:
                return
            if self._lock.locked():
                continue
            try:
                await self.async_refresh_status()
            except HomeAssistantError as err:
                _LOGGER.debug("lettura dopo l'accensione non riuscita (%ss): %s", quando, err)

    async def _async_update_data(self) -> PrinterState:
        """Manual refresh ("Aggiorna stato"): connect, read status, disconnect."""
        await self.async_refresh_status()
        return self.data

    @callback
    def _on_status(self, st: Status) -> None:
        prev = self.data.status
        # col coperchio aperto la stampante dice anche «carta finita» (0x2903, 25 ottobre): lì non conta
        finita = st.paper_out and not st.cover_open
        prima_finita = prev is not None and prev.paper_out and not prev.cover_open
        if finita and not prima_finita:
            self._roll_paper_out()
        if prima_finita and not st.paper_out:
            self._roll_maybe_changed("carta")            # era finita e adesso c'è carta: un rotolo nuovo dentro
        if st.cover_open and (prev is None or not prev.cover_open):
            self._roll_maybe_changed("coperchio")
        # originale o no (il tag): ricordato anche da spenta, così un rotolo cambiato a stampante spenta si vede alla
        # prima accensione. Con la carta finita il bit non conta
        if st.paper_loaded and not st.paper_out:
            prima = self.data.original
            if prima != st.valid_paper:
                self.data.original = st.valid_paper
                if not st.valid_paper:
                    self.data.tag = None
                self._save_roll()
            if prima is not None and prima != st.valid_paper:
                self._original_changed(st.valid_paper)
        self.data.status = st
        self.data.status_time = dt_util.utcnow()
        self.data.reachable = True
        self.async_set_updated_data(self.data)

    def _ble_device(self):
        dev = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if dev is None:
            self.data.reachable = False
            self.async_set_updated_data(self.data)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_reachable"
            )
        return dev

    def _raise(self, err: PrinterError) -> None:
        self.data.last_error = err.key
        if err.key == "connect":
            self.data.reachable = False
        self.async_set_updated_data(self.data)
        key = err.key if err.key in ("cover_open", "paper_out", "overheating", "connect", "write") else "generic"
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=f"err_{key}", translation_placeholders={"detail": str(err)}
        ) from err

    def proxy_disponibili(self) -> list[str]:
        """I nodi ESPHome che hanno la coda (esphome.<nodo>_orgsta_metti)"""
        return sorted(s[: -len("_orgsta_metti")] for s in self.hass.services.async_services().get("esphome", {})
                      if s.endswith("_orgsta_metti"))

    async def async_refresh_status(self) -> Status | None:
        async with self._lock:
            if self.proxy_fisso:
                # col proxy fisso HA non si collega mai da sé: lo stato è l'ultimo che ha detto il proxy
                return self.data.status
            if time.monotonic() < self._ponte_fino:
                # un proxy è ancora collegato a lei: lo stato è quello che ha appena detto il proxy
                return self.data.status
            dev = self._ble_device()
            builder = JobBuilder()
            try:
                async with S001Session(dev, self._on_status) as s:
                    st = await s.query_status(builder)
                    await self._read_tag(s, builder)
            except PrinterError as err:
                self._raise(err)
            self.data.last_error = None
            self.async_set_updated_data(self.data)
            return st

    async def async_preview(self, spec: LabelSpec) -> bytes:
        return (await self.async_preview_info(spec))[0]

    async def async_preview_info(self, spec: LabelSpec) -> tuple[bytes, float]:
        """(PNG, lunghezza in mm) dell'anteprima (0.8.0: sul continuo ogni etichetta ha la sua lunghezza)"""
        landscape = await self.hass.async_add_executor_job(render, spec)
        png = await self.hass.async_add_executor_job(preview_png, landscape)
        self.data.preview = png
        self.data.preview_time = dt_util.utcnow()
        self.async_set_updated_data(self.data)
        return png, round(landscape.width / 8, 1)

    async def async_print(self, spec: LabelSpec, copies: int = 1, density: int | None = None) -> None:
        await self.async_print_many([(spec, copies)], density)

    async def async_print_many(self, specs: list[tuple[LabelSpec, int]], density: int | None = None) -> int:
        """Una o più etichette in una stampa sola (0.7.0). Tutte con la carta della prima. Torna quante ne sono
        uscite. Prima prova la coda di un proxy ESPHome vicino (ponte.py), se no stampa in diretta come prima."""
        if not specs:
            return 0
        paper = PAPERS[specs[0][0].paper]
        pagine: list[Pagina] = []
        for spec, copies in specs:
            landscape = await self.hass.async_add_executor_job(render, spec)
            rows = await self.hass.async_add_executor_job(lambda l=landscape: pack_rows(to_raster(l)))
            pagine.append(Pagina(rows, max(1, int(copies))))
        last_spec = specs[-1][0]
        png = await self.hass.async_add_executor_job(
            preview_png, await self.hass.async_add_executor_job(render, last_spec))
        self.data.preview = png
        self.data.preview_time = dt_util.utcnow()
        dens = int(density or self.density)
        start = max(0, tag_start(paper) + int(self.offset))
        async with self._lock:
            self.data.busy = True
            self.async_set_updated_data(self.data)
            t0 = time.monotonic()
            try:
                self.data.last_via = {}
                uscite = await self._stampa_coda(pagine, paper.mode, dens, start)
                if uscite is None and self.proxy_fisso:
                    raise PrinterError("connect", f"proxy fisso {self.proxy_fisso} non raggiungibile")
                if uscite is None:
                    self.data.last_via = {"via": "diretta", "da": self._proxy_diretto()}
                    uscite = await self._stampa_diretta(pagine, paper.mode, dens, start)
                self.data.last_via["secondi"] = round(time.monotonic() - t0, 1)
                self.data.last_via["byte"] = sum(len(p.rows) for p in pagine)
            except PrinterError as err:
                self.data.busy = False
                self._raise(err)
            finally:
                self.data.busy = False
            self.data.last_print = dt_util.utcnow()
            self.data.last_text = " / ".join(
                x for x in (last_spec.title, last_spec.subtitle, last_spec.ssid) if x) or last_spec.layout
            self.data.labels_printed += uscite
            self.data.roll_used += uscite
            if paper.length_px is None:                       # 0.8.0: i millimetri del continuo, coda compresa
                self.data.roll_mm += sum((len(p.rows) / ROW_BYTES / 8 + 8) * p.copie for p in pagine)
            self._save_roll()
            self.data.last_error = None
            self.async_set_updated_data(self.data)
            return uscite

    async def _stampa_coda(self, pagine: list[Pagina], mode: str, density: int, start: int) -> int | None:
        """Col proxy che la sente meglio e ha la coda; un altro se il primo non la raggiunge. None = nessuno."""
        provati = 0
        candidati = proxy_con_coda(self.hass, self.address)[:3]
        if self.proxy_fisso:
            if self.proxy_fisso not in self.proxy_disponibili():
                return None
            rssi = next((p.rssi for p in candidati if p.nodo == self.proxy_fisso), -100)
            candidati = [Proxy(self.proxy_fisso, self.proxy_fisso.replace("_", "-"), rssi)]
        for proxy in candidati:
            ponte = Ponte(self.hass, proxy, self._on_status)
            provati += 1
            try:
                _LOGGER.debug("S001 %s: stampa dalla coda di %s (rssi %s)", self.address, proxy.nome, proxy.rssi)
                self.data.last_via = {"via": "coda", "da": proxy.nome, "rssi": proxy.rssi}
                uscite = await ponte.stampa(pagine, mode=mode, density=density, speed=DEFAULT_SPEED, start=start)
                u = ponte.ultimo
                self.data.last_via.update({k: u[k] for k in ("mtu", "ms_invio", "pausa_max", "pezzi", "rifatte")
                                           if k in u})
            except PrinterError as err:
                if err.key == "connect" and not ponte.fatte and not self.proxy_fisso:
                    # 0.8.3: non ci arriva (anche dopo averle prese): si tolgono dalla sua coda e si prova il prossimo
                    if ponte.consegnate:
                        try:
                            await ponte.annulla()
                        except Exception:  # noqa: BLE001
                            pass
                    _LOGGER.info("S001 %s: %s non ci arriva (%s), provo un altro", self.address, proxy.nome, err)
                    continue
                if ponte.fatte:
                    # quelle già uscite si contano anche se poi si è fermata
                    self.data.labels_printed += ponte.pagine
                    self.data.roll_used += ponte.pagine
                    self._save_roll()
                raise
            self._ponte_fino = time.monotonic() + 12
            self.data.reachable = True
            return uscite
        if provati:
            _LOGGER.info("S001 %s: nessun proxy con la coda la raggiunge, stampo in diretta", self.address)
        return None

    def _proxy_diretto(self) -> str | None:
        """Il proxy (o l'adattatore) da cui HA si collega in diretta: quello che la sente meglio"""
        try:
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            return f"{info.source} ({info.rssi})" if info else None
        except Exception:  # noqa: BLE001
            return None

    async def _stampa_diretta(self, pagine: list[Pagina], mode: str, density: int, start: int) -> int:
        """Com'era fino alla 0.6: HA collegato alla stampantina, pezzo per pezzo."""
        if time.monotonic() < self._ponte_fino:
            await asyncio.sleep(max(0.0, self._ponte_fino - time.monotonic()))
        dev = self._ble_device()
        builder = JobBuilder(paper_mode=mode, density=density, start=start)
        n = sum(p.copie for p in pagine)
        fatte = 0
        async with S001Session(dev, self._on_status) as s:
            st = await s.query_status(builder)
            check_ready(st)
            await self._read_tag(s, builder)
            for p in pagine:
                for _ in range(p.copie):
                    await s.send_job(builder.build_page(p.rows, first=fatte == 0, last=fatte == n - 1))
                    fatte += 1
        return fatte

    async def async_feed(self) -> None:
        paper = PAPERS[self.paper]
        async with self._lock:
            if time.monotonic() < self._ponte_fino:
                await asyncio.sleep(max(0.0, self._ponte_fino - time.monotonic()))
            dev = self._ble_device()
            builder = JobBuilder(paper_mode=paper.mode)
            try:
                async with S001Session(dev, self._on_status) as s:
                    st = await s.query_status(builder)
                    check_ready(st)
                    await s.send_job(builder.feed_label(), wait=False)
                    await asyncio.sleep(2)
            except PrinterError as err:
                self._raise(err)
            # anche un'etichetta vuota esce dal rotolo
            self.data.roll_used += 1
            self._save_roll()
            self.async_set_updated_data(self.data)
