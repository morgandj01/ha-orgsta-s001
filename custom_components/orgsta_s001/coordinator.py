"""State holder and print queue for one S001 printer."""

from __future__ import annotations

import asyncio
import logging
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
from .printer import PrinterError, S001Session, check_ready
from .protocol import TAG_START, JobBuilder, Status, pack_rows
from .render import DEFAULT_PAPER, PAPERS, LabelSpec, preview_png, render, to_raster

_LOGGER = logging.getLogger(__name__)


@dataclass
class PrinterState:
    status: Status | None = None
    status_time: datetime | None = None
    last_print: datetime | None = None
    last_text: str | None = None
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

    @property
    def roll_left(self) -> int | None:
        if not self.roll_counted or not self.roll_capacity:
            return None
        return max(0, self.roll_capacity - self.roll_used)


# Sotto queste etichette un rotolo «finito» non insegna niente (carta tolta a metà, sensore che sbaglia)
ROLL_MIN_LEARN = 10
# i colori dei rotoli (il nome è anche l'opzione del menu «Rotolo montato»)
ROLL_COLORS = {"bianco": "#ffffff", "azzurro": "#7fd3ff", "giallo": "#ffe14d", "rosa": "#ffaecf"}
ROLL_KINDS = ("new", "started", "same", "unknown")


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
        self.data = PrinterState()
        self._auto_task: asyncio.Task | None = None
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{address.replace(':', '').lower()}")

    # ---- rotolo: etichette usate da quando è stato messo, capienza imparata quando finisce ----
    async def async_load_roll(self) -> None:
        data = await self._store.async_load() or {}
        d = self.data
        d.roll_used = int(data.get("used") or 0)
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

    @callback
    def _save_roll(self) -> None:
        d = self.data
        self._store.async_delay_save(
            lambda: {
                "used": d.roll_used,
                "capacity": d.roll_capacity,
                "counted": d.roll_counted,
                "changed": d.roll_changed.isoformat() if d.roll_changed else None,
                "color": d.roll_color,
                "finished": d.roll_finished,
                "stock": d.stock,
                "started": d.started,
                "check": d.roll_check,
                "check_time": d.roll_check_time.isoformat() if d.roll_check_time else None,
            },
            1,
        )

    @callback
    def async_new_roll(self) -> None:
        """«Rotolo nuovo»: un rotolo nuovo dello stesso colore di quello dentro (dal cassetto)."""
        self.async_change_roll(self.data.roll_color, "new")

    @callback
    def async_change_roll(self, color: str, kind: str) -> dict:
        """Il rotolo cambiato: `new` uno nuovo dal cassetto (si conta da zero), `started` uno tolto a metà rimesso
        (riparte dalle etichette che gli restavano), `unknown` uno iniziato di cui non si sa niente, `same` è ancora
        quello di prima (si toglie solo il «da confermare"). Quello che c'era, se non era finito e si sapeva quante
        ne restavano, va tra gli iniziati del suo colore."""
        if color not in ROLL_COLORS:
            raise ValueError(f"colore sconosciuto: {color}")
        if kind not in ROLL_KINDS:
            raise ValueError(f"cambio sconosciuto: {kind}")
        d = self.data
        if kind != "same":
            left = d.roll_left
            if not d.roll_finished and left:
                d.started.setdefault(d.roll_color, []).append(int(left))
            if kind == "new":
                d.stock[color] = max(0, int(d.stock.get(color) or 0) - 1)
                d.roll_used, d.roll_counted = 0, True
            elif kind == "started" and d.started.get(color):
                left = d.started[color].pop()
                d.roll_counted = bool(d.roll_capacity)
                d.roll_used = max(0, (d.roll_capacity or left) - left)
            else:
                d.roll_used, d.roll_counted = 0, False
            d.roll_color, d.roll_finished = color, False
            d.roll_changed = dt_util.utcnow()
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
                "da_confermare_dal": d.roll_check_time.isoformat() if d.roll_check_time else None}

    @callback
    def _roll_maybe_changed(self, why: str) -> None:
        d = self.data
        if d.roll_check is None or why == "carta":
            d.roll_check, d.roll_check_time = why, dt_util.utcnow()
            _LOGGER.debug("S001 %s: rotolo forse cambiato (%s)", self.address, why)
            self._save_roll()

    @callback
    def async_set_roll_capacity(self, value: int | None) -> None:
        self.data.roll_capacity = int(value) if value else None
        self._save_roll()
        self.async_set_updated_data(self.data)

    @callback
    def _roll_paper_out(self) -> None:
        """Carta finita su un rotolo contato dall'inizio: quante ne ha fatte è la capienza."""
        d = self.data
        if d.roll_counted and d.roll_used > ROLL_MIN_LEARN:
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
        """Una lettura sola dopo l'accensione: batteria, coperchio, carta."""
        await asyncio.sleep(4)
        if self._lock.locked():
            return
        for tentativo in range(2):
            try:
                await self.async_refresh_status()
                return
            except HomeAssistantError as err:
                _LOGGER.debug("lettura all'accensione non riuscita (%s): %s", tentativo + 1, err)
                await asyncio.sleep(20)

    async def _async_update_data(self) -> PrinterState:
        """Manual refresh ("Aggiorna stato"): connect, read status, disconnect."""
        await self.async_refresh_status()
        return self.data

    @callback
    def _on_status(self, st: Status) -> None:
        prev = self.data.status
        if st.paper_out and (prev is None or not prev.paper_out):
            self._roll_paper_out()
        if prev is not None and prev.paper_out and not st.paper_out:
            self._roll_maybe_changed("carta")            # era finita e adesso c'è carta: un rotolo nuovo dentro
        if st.cover_open and (prev is None or not prev.cover_open):
            self._roll_maybe_changed("coperchio")
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

    async def async_refresh_status(self) -> Status | None:
        async with self._lock:
            dev = self._ble_device()
            builder = JobBuilder()
            try:
                async with S001Session(dev, self._on_status) as s:
                    st = await s.query_status(builder)
            except PrinterError as err:
                self._raise(err)
            self.data.last_error = None
            self.async_set_updated_data(self.data)
            return st

    async def async_preview(self, spec: LabelSpec) -> bytes:
        landscape = await self.hass.async_add_executor_job(render, spec)
        png = await self.hass.async_add_executor_job(preview_png, landscape)
        self.data.preview = png
        self.data.preview_time = dt_util.utcnow()
        self.async_set_updated_data(self.data)
        return png

    async def async_print(self, spec: LabelSpec, copies: int = 1, density: int | None = None) -> None:
        paper = PAPERS[spec.paper]
        landscape = await self.hass.async_add_executor_job(render, spec)
        png = await self.hass.async_add_executor_job(preview_png, landscape)
        rows = await self.hass.async_add_executor_job(lambda: pack_rows(to_raster(landscape)))
        self.data.preview = png
        self.data.preview_time = dt_util.utcnow()
        async with self._lock:
            dev = self._ble_device()
            builder = JobBuilder(paper_mode=paper.mode, density=int(density or self.density),
                                 start=max(0, TAG_START + int(self.offset)))
            self.data.busy = True
            self.async_set_updated_data(self.data)
            try:
                async with S001Session(dev, self._on_status) as s:
                    st = await s.query_status(builder)
                    check_ready(st)
                    n = max(1, int(copies))
                    for page in range(n):
                        await s.send_job(builder.build_page(rows, first=page == 0, last=page == n - 1))
            except PrinterError as err:
                self.data.busy = False
                self._raise(err)
            finally:
                self.data.busy = False
            self.data.last_print = dt_util.utcnow()
            self.data.last_text = " / ".join(x for x in (spec.title, spec.subtitle, spec.ssid) if x) or spec.layout
            self.data.labels_printed += max(1, copies)
            self.data.roll_used += max(1, copies)
            self._save_roll()
            self.data.last_error = None
            self.async_set_updated_data(self.data)

    async def async_feed(self) -> None:
        paper = PAPERS[self.paper]
        async with self._lock:
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
