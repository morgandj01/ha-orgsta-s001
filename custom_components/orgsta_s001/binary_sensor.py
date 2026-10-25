"""Binary sensors: cover open, paper out, printing."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import S001ConfigEntry
from .entity import S001Entity


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            Flag(c, "cover_open", BinarySensorDeviceClass.OPENING, lambda st: st.cover_open),
            Flag(c, "paper_out", BinarySensorDeviceClass.PROBLEM, lambda st: st.paper_out),
            Printing(c),
            Powered(c),
            RollCounted(c),
            RollCheck(c),
            Flag(c, "paper_loaded", None, lambda st: st.paper_loaded),
            Original(c),
        ]
    )


class Flag(S001Entity, BinarySensorEntity):
    def __init__(self, c, key, dc, fn) -> None:
        super().__init__(c, key)
        self._attr_device_class = dc
        self._fn = fn

    @property
    def is_on(self):
        st = self.coordinator.data.status
        return None if st is None else self._fn(st)


class Printing(S001Entity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, c) -> None:
        super().__init__(c, "printing")

    @property
    def is_on(self):
        d = self.coordinator.data
        return bool(d.busy or (d.status and d.status.printing))


class Powered(S001Entity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, c) -> None:
        super().__init__(c, "powered")

    @property
    def is_on(self):
        return self.coordinator.data.powered


class RollCounted(S001Entity, BinarySensorEntity):
    """Acceso se il rotolo dentro è contato dalla prima etichetta (serve per imparare la capienza)."""

    def __init__(self, c) -> None:
        super().__init__(c, "roll_counted")

    @property
    def is_on(self):
        return self.coordinator.data.roll_counted


class RollCheck(S001Entity, BinarySensorEntity):
    """Acceso se la stampante dice che il rotolo forse è stato cambiato (coperchio aperto, o carta finita e poi di
    nuovo carta) e nessuno ha ancora detto quale c'è dentro (0.5.0)."""

    def __init__(self, c) -> None:
        super().__init__(c, "roll_check")

    @property
    def is_on(self):
        return self.coordinator.data.roll_check is not None

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        return {"perche": d.roll_check, "dal": d.roll_check_time.isoformat() if d.roll_check_time else None}


class Original(S001Entity, BinarySensorEntity):
    """Acceso se dentro c'è un rotolo originale: la stampante ha letto il suo tag NFC (0.5.1). Ricordato anche a
    stampante spenta; gli attributi sono i 4 byte del tag (tipo, larghezza, lunghezza, colore)."""

    def __init__(self, c) -> None:
        super().__init__(c, "original")

    @property
    def is_on(self):
        return self.coordinator.data.original

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data
        return {**(d.tag or {}), "letto_il": d.tag_time.isoformat() if d.tag_time else None}
