"""Config flow for Vividmoo."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import build_api
from .api import VividmooAuthError, VividmooError
from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNT_TYPE,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_CLIENT_DEVICE_ID,
    CONF_ENABLE_MQTT,
    CONF_MQTT_VERIFY_TLS,
    CONF_PASSWORD,
    CONF_PASSWORD_MODE,
    DEFAULT_ACCOUNT_TYPE,
    DEFAULT_BASE_URL,
    DEFAULT_ENABLE_MQTT,
    DEFAULT_PASSWORD_MODE,
    DOMAIN,
    PASSWORD_MODE_MD5,
    PASSWORD_MODE_PLAIN,
)

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=defaults.get(CONF_ACCOUNT, "")): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_APP_ID, default=defaults.get(CONF_APP_ID, vol.UNDEFINED)): str,
            vol.Required(CONF_APP_KEY, default=defaults.get(CONF_APP_KEY, vol.UNDEFINED)): str,
            vol.Required(CONF_ACCOUNT_TYPE, default=defaults.get(CONF_ACCOUNT_TYPE, DEFAULT_ACCOUNT_TYPE)): vol.Coerce(int),
            vol.Required(CONF_PASSWORD_MODE, default=defaults.get(CONF_PASSWORD_MODE, DEFAULT_PASSWORD_MODE)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[PASSWORD_MODE_PLAIN, PASSWORD_MODE_MD5], translation_key=CONF_PASSWORD_MODE)
            ),
            vol.Required(CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)): str,
            vol.Optional(CONF_CLIENT_DEVICE_ID, default=defaults.get(CONF_CLIENT_DEVICE_ID, "")): str,
        }
    )


class VividmooConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data: dict[str, Any]) -> dict[str, str]:
        api = build_api(self.hass, data)
        try:
            await api.sync_clock()
            await api.login()
            await api.devices()
        except VividmooAuthError as err:
            _LOGGER.warning("Vividmoo login rejected: %s", err)
            return {"base": "invalid_auth"}
        except VividmooError as err:
            _LOGGER.warning("Vividmoo login failed: %s", err)
            return {"base": "cannot_connect"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Stable per-install client id; the app uses 32 hex chars (android_id-derived).
            user_input[CONF_CLIENT_DEVICE_ID] = user_input.get(CONF_CLIENT_DEVICE_ID) or secrets.token_hex(16)
            await self.async_set_unique_id(user_input[CONF_ACCOUNT].lower())
            self._abort_if_unique_id_configured()
            errors = await self._validate(user_input)
            if not errors:
                return self.async_create_entry(title=user_input[CONF_ACCOUNT], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            errors = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reauth_confirm", data_schema=_schema(entry.data), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VividmooOptionsFlow()


class VividmooOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENABLE_MQTT, default=options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)): bool,
                    vol.Required(CONF_MQTT_VERIFY_TLS, default=options.get(CONF_MQTT_VERIFY_TLS, True)): bool,
                }
            ),
        )
