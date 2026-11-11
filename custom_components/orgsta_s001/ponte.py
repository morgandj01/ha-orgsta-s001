"""La stampa passando dalla coda dentro un proxy ESPHome (0.7.0, `esphome/stampantina.yaml` del repository).

Prima (fino a 0.6) HA mandava la stampa in diretta, un pezzetto da 128 byte alla volta: HA → Wi-Fi → proxy →
Bluetooth → stampantina. Un attimo di Wi-Fi, il proxy occupato a cercare telefoni per Bermuda, e la stampa si fermava
a metà (mezza etichetta). Ora HA consegna le etichette intere, compresse, al proxy che sente meglio la stampantina
(`orgsta_metti`, con la risposta: arrivata o no) e il proxy le stampa da solo, collegato a pochi metri. Il Wi-Fi può
cadere: le etichette già consegnate escono lo stesso, e una pagina interrotta si rifà da capo sul proxy.

Senza nessun proxy con la coda si stampa come prima (printer.S001Session).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
import zlib
from dataclasses import dataclass

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .printer import PrinterError
from .protocol import PaperMode, Frame, Status, parse_status

_LOGGER = logging.getLogger(__name__)

AZIONE = "orgsta_metti"
STATO = "orgsta_stato"
ANNULLA = "orgsta_annulla"
CHIAMATA_S = 12.0          # una chiamata all'ESP (consegna di un'etichetta o lo stato)
PIENA_S = 1.0              # coda piena: si riprova appena se ne stampa una
GIRO_S = 0.8               # ogni quanto si guarda a che punto è
PAGINA_S = 60.0            # al massimo per pagina, compreso il collegamento
SENZA_RISPOSTA_S = 90.0    # il proxy non risponde (Wi-Fi giù): dopo tanto ci si arrende
RESTA_S = 8                # il proxy resta collegato alla stampantina per la prossima etichetta
RSSI_MIN = -100            # sotto non si prova nemmeno

MODI = {PaperMode.PLAIN: 0, PaperMode.TAG: 1, PaperMode.BLACK_TAG: 2}
ERRORI = {"cover_open", "paper_out", "overheating", "connect", "write"}


@dataclass
class Pagina:
    """Un'etichetta da mandare: il raster già fatto e quante copie."""

    rows: bytes
    copie: int = 1


@dataclass
class Proxy:
    nodo: str          # il nome ESPHome con «_» (quello dei servizi esphome.<nodo>_orgsta_metti)
    nome: str
    rssi: int


def _servizio(nodo: str, azione: str) -> str:
    return f"{nodo}_{azione}"



def _mac_meno(mac: str, n: int) -> str | None:
    try:
        v = int(mac.replace(":", ""), 16) - n
    except ValueError:
        return None
    if v < 0:
        return None
    h = f"{v:012x}"
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _dispositivi(devreg, source: str) -> list:
    """I dispositivi di HA che possono essere il proxy con quel MAC Bluetooth (0.8.3: tutti, anche quando lo stesso MAC
    sta in più dispositivi, per esempio uno di UniFi e uno di ESPHome)."""
    macs = {(dr.CONNECTION_BLUETOOTH, source.lower()), (dr.CONNECTION_NETWORK_MAC, source.lower())}
    wifi = _mac_meno(source, 2)
    if wifi:
        macs.add((dr.CONNECTION_NETWORK_MAC, wifi.lower()))
    return [d for d in devreg.devices.values()
            if any((k, str(m).lower()) in macs for k, m in d.connections)]


def _nodo_con_coda(hass: HomeAssistant, dev) -> str | None:
    """Il nome del nodo ESPHome che ha l'azione della coda. Il nome nella configurazione di HA può essere vecchio
    (ble-frigorifero era nato come irk-capture): si prova anche il nome del dispositivo e il titolo"""
    for eid in getattr(dev, "config_entries", None) or [dev.config_entry_id]:
        entry = hass.config_entries.async_get_entry(eid)
        if entry is None or entry.domain != "esphome":
            continue
        for nome in (entry.data.get("device_name"), dev.name, entry.title):
            nodo = str(nome or "").strip().lower().replace("-", "_").replace(" ", "_")
            if nodo and hass.services.has_service("esphome", _servizio(nodo, AZIONE)):
                return nodo
    return None


def proxy_con_coda(hass: HomeAssistant, address: str) -> list[Proxy]:
    """I proxy che sentono la stampantina e hanno la coda, dal più vicino (RSSI migliore)."""
    devreg = dr.async_get(hass)
    visti: dict[str, Proxy] = {}
    try:
        sentiti = bluetooth.async_scanner_devices_by_address(hass, address, connectable=False)
    except Exception:  # noqa: BLE001
        sentiti = []
    for s in sentiti:
        source = getattr(s.scanner, "source", None)
        rssi = getattr(s.advertisement, "rssi", None)
        if not source or rssi is None or rssi < RSSI_MIN:
            continue
        # 0.8.2: la sorgente è il MAC Bluetooth del proxy. Quel MAC in HA è spesso un dispositivo dell'integrazione
        # bluetooth (non esphome), e il dispositivo ESPHome ha il MAC del Wi-Fi, che sull'ESP32 è quello Bluetooth
        # meno 2. Si guardano tutti e tre e vale il primo che è di ESPHome e ha la coda
        for dev in _dispositivi(devreg, source):
            nodo = _nodo_con_coda(hass, dev)
            if nodo:
                break
        else:
            continue
        if nodo:
            p = Proxy(nodo, dev.name_by_user or dev.name or nodo, int(rssi))
            if nodo not in visti or visti[nodo].rssi < p.rssi:
                visti[nodo] = p
    return sorted(visti.values(), key=lambda p: -p.rssi)


def stato_da(risposta: dict) -> Status | None:
    """Lo stato della stampante come lo legge l'integrazione, dall'ultima risposta che il proxy ha visto."""
    st, cmd = risposta.get("st"), risposta.get("st_cmd")
    if not st or cmd is None:
        return None
    try:
        return parse_status(Frame(cmd=int(cmd), seq=0, payload=bytes.fromhex(st)))
    except (ValueError, TypeError):
        return None


class Ponte:
    """Una stampa (una o più etichette) consegnata a un proxy con la coda."""

    def __init__(self, hass: HomeAssistant, proxy: Proxy, on_status) -> None:
        self.hass = hass
        self.proxy = proxy
        self._on_status = on_status
        self.lavoro = uuid.uuid4().hex[:10]
        self.consegnate = 0
        self.fatte = 0
        self.pagine = 0
        self.ultimo: dict = {}        # l'ultima risposta del proxy (0.8.1: mtu, tempi dell'ultima pagina)

    async def _chiama(self, azione: str, dati: dict | None = None) -> dict:
        resp = await asyncio.wait_for(
            self.hass.services.async_call(
                "esphome", _servizio(self.proxy.nodo, azione), dati or {}, blocking=True, return_response=True
            ),
            CHIAMATA_S,
        )
        r = resp or {}
        if isinstance(r, dict) and "response" in r and isinstance(r["response"], dict):
            r = r["response"]
        if isinstance(r, dict) and (st := stato_da(r)) is not None:
            try:
                self._on_status(st)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("status callback failed")
        if isinstance(r, dict) and r.get("lavoro") == self.lavoro:
            self.ultimo = r
            self.fatte = int(r.get("fatte") or 0)
            self.pagine = int(r.get("pagine") or 0)
        return r if isinstance(r, dict) else {}

    def _errore(self, r: dict) -> PrinterError:
        chiave = str(r.get("errore") or r.get("perche") or "write")
        return PrinterError(chiave if chiave in ERRORI else "write", f"proxy {self.proxy.nome}: {chiave}")

    async def stampa(self, pagine: list[Pagina], *, mode: str, density: int, speed: int, start: int) -> int:
        """Consegna e aspetta la fine. Torna le pagine stampate; PrinterError se la stampante si ferma.
        `consegnate` dice quante etichette il proxy ha già preso (0 = si può provare un altro proxy)."""
        totale = len(pagine)
        modo = MODI.get(mode, 1)
        zipped = [base64.b64encode(zlib.compress(p.rows, 9)).decode() for p in pagine]
        silenzio_da: float | None = None
        limite = time.monotonic() + 30 + PAGINA_S * sum(max(1, p.copie) for p in pagine)
        i = 0
        while True:
            if time.monotonic() > limite:
                raise PrinterError("write", f"proxy {self.proxy.nome}: troppo tempo")
            try:
                if i < totale:
                    p = pagine[i]
                    r = await self._chiama(AZIONE, {
                        "lavoro": self.lavoro, "indice": i, "totale": totale, "copie": max(1, int(p.copie)),
                        "modo": modo, "densita": int(density), "velocita": int(speed), "inizio": int(start),
                        "righe": len(p.rows) // 12, "resta": RESTA_S, "dati": zipped[i],
                    })
                else:
                    r = await self._chiama(STATO)
                silenzio_da = None
            except (HomeAssistantError, asyncio.TimeoutError, TimeoutError) as err:
                # il proxy non risponde: se ha già le etichette continua da solo, si aspetta che torni
                if self.consegnate == 0:
                    raise PrinterError("connect", f"proxy {self.proxy.nome}: {err}") from err
                silenzio_da = silenzio_da or time.monotonic()
                if time.monotonic() - silenzio_da > SENZA_RISPOSTA_S:
                    raise PrinterError("write", f"proxy {self.proxy.nome} sparito: {err}") from err
                await asyncio.sleep(2)
                continue

            if i < totale:
                if r.get("ok"):
                    i = max(i + 1, int(r.get("ricevute") or 0))
                    self.consegnate = i
                    continue
                perche = r.get("perche")
                if perche == "piena":
                    await asyncio.sleep(PIENA_S)
                    continue
                if perche == "ordine":
                    i = int(r.get("ricevute") or 0)
                    continue
                if perche == "occupata":
                    if self.consegnate == 0:
                        raise PrinterError("connect", f"proxy {self.proxy.nome} occupato con un'altra stampa")
                    await asyncio.sleep(PIENA_S)
                    continue
                raise self._errore(r)

            if r.get("lavoro") != self.lavoro:
                # il proxy si è riavviato: le etichette che aveva le ha perse
                raise PrinterError("write", f"proxy {self.proxy.nome} ripartito a metà")
            fase = r.get("fase")
            if fase == "fatto" or (fase in ("libera",) and self.fatte >= totale):
                return self.pagine
            if fase == "errore":
                raise self._errore(r)
            await asyncio.sleep(GIRO_S)

    async def annulla(self) -> None:
        try:
            await self._chiama(ANNULLA, {"lavoro": self.lavoro})
        except (HomeAssistantError, asyncio.TimeoutError, TimeoutError):
            pass
