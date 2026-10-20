"""Buttons: refresh status, feed a label, print quick text, test label."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import S001ConfigEntry
from .entity import S001Entity
from .render import LabelError, LabelSpec


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([Refresh(c), Feed(c), PrintQuick(c), PrintTest(c), NewRoll(c)])


class Refresh(S001Entity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, c) -> None:
        super().__init__(c, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_refresh_status()


class Feed(S001Entity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c) -> None:
        super().__init__(c, "feed")

    async def async_press(self) -> None:
        await self.coordinator.async_feed()


class PrintQuick(S001Entity, ButtonEntity):
    def __init__(self, c) -> None:
        super().__init__(c, "print_quick")

    async def async_press(self) -> None:
        text = (self.coordinator.quick_text or "").strip()
        if not text:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_text")
        title, _, sub = text.partition("|")
        try:
            await self.coordinator.async_print(LabelSpec(title=title.strip(), subtitle=sub.strip(), paper=self.coordinator.paper))
        except LabelError as err:
            raise ServiceValidationError(str(err)) from err


class PrintTest(S001Entity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, c) -> None:
        super().__init__(c, "print_test")

    async def async_press(self) -> None:
        await self.coordinator.async_print(
            LabelSpec(layout="icona", icon="mdi:robot-happy", title="LOCO", subtitle="stampante pronta", paper=self.coordinator.paper)
        )


class NewRoll(S001Entity, ButtonEntity):
    """Rotolo nuovo: il conto riparte da zero e alla fine si impara quante ne tiene."""

    def __init__(self, c) -> None:
        super().__init__(c, "roll_new")

    async def async_press(self) -> None:
        self.coordinator.async_new_roll()
