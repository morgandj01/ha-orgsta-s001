"""Loaded paper roll."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import ROLL_COLORS, S001ConfigEntry
from .entity import S001Entity
from .render import PAPERS


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Paper(entry.runtime_data), RollColor(entry.runtime_data)])


class Paper(S001Entity, SelectEntity, RestoreEntity):
    _attr_options = list(PAPERS)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "paper")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # dalla 0.5.1 la carta sta nel registro del rotolo; lo stato vecchio serve solo la prima volta
        if "paper" not in self.coordinator.paper_saved and (last := await self.async_get_last_state()) \
                and last.state in PAPERS:
            self.coordinator.paper = last.state

    @property
    def current_option(self) -> str:
        return self.coordinator.paper

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_set_paper(option)


class RollColor(S001Entity, SelectEntity):
    """Il colore del rotolo dentro (0.5.0). Cambiarlo qui lo corregge e basta: per un cambio di rotolo vero c'è
    il servizio «Cambia rotolo» (scala il cassetto e si ricorda quelli tolti a metà)."""

    _attr_options = list(ROLL_COLORS)

    def __init__(self, c) -> None:
        super().__init__(c, "roll_color")

    @property
    def current_option(self) -> str:
        return self.coordinator.data.roll_color

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_set_color(option)
