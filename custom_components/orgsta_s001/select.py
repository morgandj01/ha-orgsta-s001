"""Loaded paper roll."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import S001ConfigEntry
from .entity import S001Entity
from .render import PAPERS


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Paper(entry.runtime_data)])


class Paper(S001Entity, SelectEntity, RestoreEntity):
    _attr_options = list(PAPERS)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "paper")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in PAPERS:
            self.coordinator.paper = last.state

    @property
    def current_option(self) -> str:
        return self.coordinator.paper

    async def async_select_option(self, option: str) -> None:
        self.coordinator.paper = option
        self.async_write_ha_state()
