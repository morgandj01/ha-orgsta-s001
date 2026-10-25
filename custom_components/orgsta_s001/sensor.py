"""Sensors: battery, status, last print, labels printed."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import S001ConfigEntry
from .entity import S001Entity

STATES = ["pronta", "spenta", "in_stampa", "coperchio_aperto", "carta_finita", "surriscaldata", "non_raggiungibile", "sconosciuto"]


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([BatterySensor(c), StateSensor(c), LastPrintSensor(c), CountSensor(c), RollUsedSensor(c), RollLeftSensor(c), AreaSensor(c)])


class BatterySensor(S001Entity, SensorEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, c) -> None:
        super().__init__(c, "battery")
        self._restored = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            try:
                self._restored = int(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self):
        st = self.coordinator.data.status
        if st and st.battery is not None:
            return st.battery
        return self._restored


class StateSensor(S001Entity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES

    def __init__(self, c) -> None:
        super().__init__(c, "state")

    @property
    def native_value(self):
        d = self.coordinator.data
        if d.busy:
            return "in_stampa"
        st = d.status
        if d.powered is False:
            return "spenta"
        if d.reachable is False:
            return "non_raggiungibile"
        if st is None:
            return "sconosciuto"
        if st.cover_open:
            return "coperchio_aperto"
        if st.paper_out:
            return "carta_finita"
        if st.overheating:
            return "surriscaldata"
        if st.printing:
            return "in_stampa"
        return "pronta"

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        st = d.status
        return {
            "ultimo_contatto": d.status_time.isoformat() if d.status_time else None,
            "ultimo_annuncio": d.seen_time.isoformat() if d.seen_time else None,
            "ultimo_errore": d.last_error,
            "intensita_stampante": st.density if st else None,
            "bit_stato": f"0x{st.bits:04x}" if st else None,
        }


class LastPrintSensor(S001Entity, SensorEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, c) -> None:
        super().__init__(c, "last_print")
        self._restored = None
        self._restored_attrs = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            from homeassistant.util import dt as dt_util

            self._restored = dt_util.parse_datetime(last.state)
            self._restored_attrs = dict(last.attributes)

    @property
    def native_value(self):
        return self.coordinator.data.last_print or self._restored

    @property
    def extra_state_attributes(self):
        t = self.coordinator.data.last_text or self._restored_attrs.get("contenuto")
        return {"contenuto": t}


class CountSensor(S001Entity, SensorEntity, RestoreEntity):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "etichette"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, c) -> None:
        super().__init__(c, "labels_printed")
        self._base = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            try:
                self._base = int(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self):
        return self._base + self.coordinator.data.labels_printed


class RollUsedSensor(S001Entity, SensorEntity):
    """Etichette uscite dal rotolo dentro (da quando è stato premuto «Rotolo nuovo»)."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "etichette"

    def __init__(self, c) -> None:
        super().__init__(c, "roll_used")

    @property
    def native_value(self):
        return self.coordinator.data.roll_used

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        info = self.coordinator.roll_info()
        return {
            "capienza": d.roll_capacity,
            "rimaste": d.roll_left,
            "contato_da_nuovo": d.roll_counted,
            "cambiato_il": d.roll_changed.isoformat() if d.roll_changed else None,
            "colore": info["colore"],
            "finito": info["finito"],
            "scorte": info["scorte"],
            "iniziati": info["iniziati"],
            "da_confermare": info["da_confermare"],
        }


class RollLeftSensor(S001Entity, SensorEntity):
    """Etichette che restano: si sa solo con la capienza imparata e il rotolo contato dall'inizio."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "etichette"

    def __init__(self, c) -> None:
        super().__init__(c, "roll_left")

    @property
    def native_value(self):
        return self.coordinator.data.roll_left


class AreaSensor(S001Entity, SensorEntity, RestoreEntity):
    """La stanza dove sta: l'area del proxy Bluetooth che la sente meglio (solo da accesa)."""

    def __init__(self, c) -> None:
        super().__init__(c, "area")
        self._restored = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            self._restored = last.state

    @property
    def native_value(self):
        return self.coordinator.data.area or self._restored

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        return {"area_id": d.area_id, "proxy": d.proxy, "proxy_mac": d.proxy_address, "rssi": d.rssi}
