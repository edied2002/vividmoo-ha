"""Vividmoo (UBTech airpet) cat litter box integration."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import DATA_EXTRA_JS_URL_ES5, DATA_EXTRA_MODULE_URL, add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import VividmooApi
from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNT_TYPE,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_CLIENT_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PASSWORD_MODE,
    DOMAIN,
)
from .coordinator import VividmooCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_URL = "/vividmoo/vividmoo-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "vividmoo-card.js"
DATA_CARD = f"{DOMAIN}_card_registered"

type VividmooConfigEntry = ConfigEntry[VividmooCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the Lovelace card from the integration and load it on every dashboard."""
    await async_register_card(hass)
    return True


async def async_register_card(hass: HomeAssistant) -> bool:
    """Register the static path and load the card on every dashboard, once per Home Assistant start.

    The frontend's index.html imports `extra_modules` on every browser, but browsers that fail its
    `isModern` check (older Android WebViews in the companion app among them) get the legacy
    "es5" build, which additionally loads `extra_js_es5` as classic <script>s. The card file is
    written to run both ways, so it is registered for both. The ?v= content hash makes every
    card change a new URL, so cached old copies (e.g. in the companion app's WebView) aren't reused.
    Returns False if http/frontend aren't loaded.
    """
    if hass.data.get(DATA_CARD):
        return True
    if hass.http is None or DATA_EXTRA_MODULE_URL not in hass.data or DATA_EXTRA_JS_URL_ES5 not in hass.data:
        _LOGGER.debug("http/frontend not loaded; vividmoo-card not registered")
        return False
    digest = await hass.async_add_executor_job(_card_digest)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)])
    url = f"{CARD_URL}?v={digest}"
    add_extra_js_url(hass, url)  # modern build: import()
    add_extra_js_url(hass, url, es5=True)  # legacy build: classic <script>
    hass.data[DATA_CARD] = url
    _LOGGER.debug("vividmoo-card registered at %s", url)
    return True


def _card_digest() -> str:
    return hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:12]


def build_api(hass: HomeAssistant, data: dict) -> VividmooApi:
    return VividmooApi(
        async_get_clientsession(hass),
        base_url=data[CONF_BASE_URL],
        app_id=data[CONF_APP_ID],
        app_key=data[CONF_APP_KEY],
        client_device_id=data[CONF_CLIENT_DEVICE_ID],
        account=data[CONF_ACCOUNT],
        password=data[CONF_PASSWORD],
        account_type=data[CONF_ACCOUNT_TYPE],
        password_mode=data[CONF_PASSWORD_MODE],
    )


async def async_setup_entry(hass: HomeAssistant, entry: VividmooConfigEntry) -> bool:
    api = build_api(hass, dict(entry.data))
    await api.sync_clock()
    coordinator = VividmooCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VividmooConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: VividmooConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
