"""Default print density."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_DENSITY
from .coordinator import ROLL_COLORS, S001ConfigEntry
from .entity import S001Entity


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([Density(c), RollCapacity(c), PrintOffset(c)] + [RollStock(c, col) for col in ROLL_COLORS])


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


class PrintOffset(S001Entity, RestoreNumber):
    """Sposta la stampa lungo l'etichetta per centrarla (8 punti = 1 mm; positivo = verso l'inizio del testo)."""

    _attr_native_min_value = -24
    _attr_native_max_value = 40
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "punti"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "print_offset")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value is not None:
            self.coordinator.offset = int(last.native_value)

    @property
    def native_value(self) -> float:
        return self.coordinator.offset

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.offset = int(value)
        self.async_write_ha_state()


class RollStock(S001Entity, NumberEntity):
    """Quanti rotoli nuovi di un colore ci sono nel cassetto (0.5.0): scende da solo quando se ne mette uno."""

    _attr_native_min_value = 0
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "rotoli"

    def __init__(self, c, color: str) -> None:
        super().__init__(c, f"stock_{color}")
        self._color = color

    @property
    def native_value(self):
        return int(self.coordinator.data.stock.get(self._color) or 0)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_stock(self._color, int(value))
