"""Config flow for the STIGA lawn mower integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StigaAPI, StigaAuthError, StigaApiError
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL):    str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL):    str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_credentials(
    hass, email: str, password: str
) -> tuple[str | None, bool]:
    """Return (error_key, has_devices). error_key is None on success."""
    session = async_get_clientsession(hass)
    api = StigaAPI(email=email, password=password, session=session)
    try:
        await api.authenticate()
        devices = await api.get_devices()
        return None, bool(devices)
    except StigaAuthError:
        return "invalid_auth", False
    except StigaApiError:
        return "cannot_connect", False
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unexpected error during credential check")
        return "unknown", False


class StigaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow: setup via the Home Assistant UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            error, has_devices = await _validate_credentials(
                self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            elif not has_devices:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show password form and update credentials on success."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            error, _ = await _validate_credentials(
                self.hass,
                reauth_entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"email": reauth_entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user change email/password without removing the entry."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            new_email = user_input[CONF_EMAIL]
            if new_email.lower() != reconfigure_entry.unique_id:
                return self.async_abort(reason="account_mismatch")

            error, has_devices = await _validate_credentials(
                self.hass, new_email, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            elif not has_devices:
                errors["base"] = "no_devices"
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data=user_input,
                    title=new_email,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA,
                {CONF_EMAIL: reconfigure_entry.data.get(CONF_EMAIL, "")},
            ),
            errors=errors,
        )
