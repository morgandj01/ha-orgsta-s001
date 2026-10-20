"""Image entity with the preview of the last label rendered or printed."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import S001ConfigEntry
from .entity import S001Entity


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Preview(hass, entry.runtime_data)])


class Preview(S001Entity, ImageEntity):
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, c) -> None:
        S001Entity.__init__(self, c, "preview")
        ImageEntity.__init__(self, hass)
        self._seen = None

    @callback
    def _handle_coordinator_update(self) -> None:
        t = self.coordinator.data.preview_time
        if t and t != self._seen:
            self._seen = t
            self._attr_image_last_updated = t
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return self.coordinator.data.preview
