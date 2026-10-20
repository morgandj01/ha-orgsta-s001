"""Quick text for the "print quick text" button. Use | to add a second line."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import S001ConfigEntry
from .entity import S001Entity


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([QuickText(entry.runtime_data)])


class QuickText(S001Entity, TextEntity, RestoreEntity):
    _attr_native_max = 80

    def __init__(self, c) -> None:
        super().__init__(c, "quick_text")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            self.coordinator.quick_text = last.state

    @property
    def native_value(self) -> str:
        return self.coordinator.quick_text

    async def async_set_value(self, value: str) -> None:
        self.coordinator.quick_text = value
        self.async_write_ha_state()
