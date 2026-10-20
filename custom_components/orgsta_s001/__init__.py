"""ORGSTA S001 (Snap & Tag) Bluetooth label printer for Home Assistant."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ALIGN,
    ATTR_BORDER,
    ATTR_COPIES,
    ATTR_DATE,
    ATTR_DATE_PREFIX,
    ATTR_DENSITY,
    ATTR_DEVICE_ID,
    ATTR_EXPIRY,
    ATTR_ICON,
    ATTR_IMAGE,
    ATTR_INVERT,
    ATTR_LAYOUT,
    ATTR_LENGTH_MM,
    ATTR_PAPER,
    ATTR_PASSWORD,
    ATTR_QR,
    ATTR_SSID,
    ATTR_SUBTITLE,
    ATTR_TITLE,
    CONF_ADDRESS,
    DOMAIN,
    MAX_COPIES,
    SERVICE_FEED,
    SERVICE_PREVIEW,
    SERVICE_PRINT,
)
from .coordinator import S001ConfigEntry, S001Coordinator
from .render import LAYOUTS, PAPERS, LabelError, LabelSpec

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

LABEL_SCHEMA = {
    vol.Optional(ATTR_DEVICE_ID): cv.string,
    vol.Optional(ATTR_LAYOUT, default="testo"): vol.In(LAYOUTS),
    vol.Optional(ATTR_TITLE, default=""): cv.string,
    vol.Optional(ATTR_SUBTITLE, default=""): cv.string,
    vol.Optional(ATTR_ICON): cv.string,
    vol.Optional(ATTR_QR): cv.string,
    vol.Optional(ATTR_DATE): cv.string,
    vol.Optional(ATTR_EXPIRY): cv.string,
    vol.Optional(ATTR_DATE_PREFIX): cv.string,
    vol.Optional(ATTR_SSID): cv.string,
    vol.Optional(ATTR_PASSWORD): cv.string,
    vol.Optional(ATTR_IMAGE): cv.string,
    vol.Optional(ATTR_BORDER, default=False): cv.boolean,
    vol.Optional(ATTR_INVERT, default=False): cv.boolean,
    vol.Optional(ATTR_ALIGN, default="center"): vol.In(["center", "left"]),
    vol.Optional(ATTR_PAPER): vol.In(list(PAPERS)),
    vol.Optional(ATTR_LENGTH_MM): vol.All(vol.Coerce(float), vol.Range(min=8, max=200)),
}
PRINT_SCHEMA = vol.Schema(
    {
        **LABEL_SCHEMA,
        vol.Optional(ATTR_COPIES, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_COPIES)),
        vol.Optional(ATTR_DENSITY): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
    }
)
PREVIEW_SCHEMA = vol.Schema(LABEL_SCHEMA)
FEED_SCHEMA = vol.Schema({vol.Optional(ATTR_DEVICE_ID): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services once."""

    def _coordinator(call: ServiceCall) -> S001Coordinator:
        entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
        if not entries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_printer")
        device_id = call.data.get(ATTR_DEVICE_ID)
        if device_id:
            dev = dr.async_get(hass).async_get(device_id)
            if dev is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_printer")
            for e in entries:
                if e.entry_id in dev.config_entries:
                    return e.runtime_data
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_printer")
        return entries[0].runtime_data

    async def _load_image(ref: str) -> bytes:
        ref = ref.strip()
        if ref.startswith(("camera.", "image.")):
            domain = ref.split(".")[0]
            try:
                if domain == "camera":
                    from homeassistant.components.camera import async_get_image

                    return (await async_get_image(hass, ref)).content
                from homeassistant.components.image import DATA_COMPONENT

                ent = hass.data[DATA_COMPONENT].get_entity(ref)
                data = await ent.async_image() if ent else None
                if not data:
                    raise HomeAssistantError("empty")
                return data
            except (HomeAssistantError, ImportError, KeyError, AttributeError) as err:
                raise ServiceValidationError(f"immagine non disponibile da {ref}") from err
        if ref.startswith(("http://", "https://")):
            session = async_get_clientsession(hass)
            async with session.get(ref, timeout=20) as resp:
                if resp.status != 200:
                    raise ServiceValidationError(f"download immagine fallito ({resp.status})")
                return await resp.read()
        if ref.startswith("/local/"):
            ref = hass.config.path("www", ref[len("/local/"):])
        path = Path(ref)
        if not path.is_absolute():
            path = Path(hass.config.path(ref))
        if not hass.config.is_allowed_path(str(path)):
            raise ServiceValidationError(f"percorso non consentito: {path} (aggiungilo ad allowlist_external_dirs)")
        return await hass.async_add_executor_job(path.read_bytes)

    async def _spec(call: ServiceCall, coord: S001Coordinator) -> LabelSpec:
        d = call.data
        image = await _load_image(d[ATTR_IMAGE]) if d.get(ATTR_IMAGE) else None
        extra: dict = {"today": dt_util.now().date()}
        if d.get(ATTR_DATE_PREFIX) is not None:
            extra["date_prefix"] = d[ATTR_DATE_PREFIX]
        return LabelSpec(
            layout=d[ATTR_LAYOUT],
            title=d.get(ATTR_TITLE, ""),
            subtitle=d.get(ATTR_SUBTITLE, ""),
            icon=d.get(ATTR_ICON),
            qr=d.get(ATTR_QR),
            date=d.get(ATTR_DATE),
            expiry=d.get(ATTR_EXPIRY),
            ssid=d.get(ATTR_SSID),
            password=d.get(ATTR_PASSWORD),
            image=image,
            border=d[ATTR_BORDER],
            invert=d[ATTR_INVERT],
            align=d[ATTR_ALIGN],
            paper=d.get(ATTR_PAPER) or coord.paper,
            length_mm=d.get(ATTR_LENGTH_MM),
            extra=extra,
        )

    async def handle_print(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(call)
        spec = await _spec(call, coord)
        try:
            await coord.async_print(spec, call.data[ATTR_COPIES], call.data.get(ATTR_DENSITY))
        except LabelError as err:
            raise ServiceValidationError(str(err)) from err
        st = coord.data.status
        return {
            "printed": call.data[ATTR_COPIES],
            "battery": st.battery if st else None,
        }

    async def handle_preview(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(call)
        spec = await _spec(call, coord)
        try:
            png = await coord.async_preview(spec)
        except LabelError as err:
            raise ServiceValidationError(str(err)) from err
        return {"png_base64": base64.b64encode(png).decode()}

    async def handle_feed(call: ServiceCall) -> None:
        await _coordinator(call).async_feed()

    hass.services.async_register(
        DOMAIN, SERVICE_PRINT, handle_print, schema=PRINT_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PREVIEW, handle_preview, schema=PREVIEW_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_FEED, handle_feed, schema=FEED_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: S001ConfigEntry) -> bool:
    coord = S001Coordinator(hass, entry, entry.data[CONF_ADDRESS])
    entry.runtime_data = coord
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: S001ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
