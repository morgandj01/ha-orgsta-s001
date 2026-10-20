"""Default print density."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_DENSITY
from .coordinator import S001ConfigEntry
from .entity import S001Entity


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Density(entry.runtime_data), RollCapacity(entry.runtime_data)])


class Density(S001Entity, RestoreNumber):
    _attr_native_min_value = 1
    _attr_native_max_value = 15
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "density")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value is not None:
            self.coordinator.density = int(last.native_value)

    @property
    def native_value(self) -> float:
        return self.coordinator.density or DEFAULT_DENSITY

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.density = int(value)
        self.async_write_ha_state()


class RollCapacity(S001Entity, NumberEntity):
    """Quante etichette tiene un rotolo: la impara da sola, si può anche scrivere (0 = non si sa)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 2000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "etichette"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "roll_capacity")

    @property
    def native_value(self):
        return self.coordinator.data.roll_capacity

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_roll_capacity(int(value))
