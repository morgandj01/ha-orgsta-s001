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
