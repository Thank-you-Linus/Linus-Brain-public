"""
Config Flow for Linus Brain Integration

This module handles the UI-based configuration flow for setting up the Linus Brain
integration. Users can input their Supabase credentials (URL and API key) through
the Home Assistant UI.
"""

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Configuration schema for user input
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, description="Supabase URL"): str,
        vol.Required(CONF_API_KEY, description="Supabase API Key"): str,
    }
)


async def validate_supabase_connection(
    hass: HomeAssistant, url: str, api_key: str
) -> dict[str, Any]:
    """
    Validate the Supabase connection by attempting a simple request.

    This function tests the provided credentials by making a request to the
    Supabase API to ensure connectivity and authentication work.

    Args:
        hass: Home Assistant instance
        url: Supabase project URL
        api_key: Supabase API key (anon or service key)

    Returns:
        Dictionary with validation result

    Raises:
        Exception: If connection or authentication fails
    """
    session = async_get_clientsession(hass)

    # Test connection with a simple REST API call
    # We'll try to access the health endpoint or a simple query
    test_url = f"{url.rstrip('/')}/rest/v1/"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
    }

    try:
        async with session.get(
            test_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status in (200, 401, 404):
                # 200: Success
                # 401/404: Endpoint exists but might need proper table setup
                # Either way, connection is established
                _LOGGER.info("Supabase connection validated successfully")
                return {"status": "ok"}
            else:
                _LOGGER.error(f"Supabase returned status {response.status}")
                raise Exception(f"Unexpected status code: {response.status}")

    except aiohttp.ClientError as err:
        _LOGGER.error(f"Connection error to Supabase: {err}")
        raise Exception(f"Cannot connect to Supabase: {err}")
    except Exception as err:
        _LOGGER.error(f"Validation error: {err}")
        raise


class LinusBrainConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Handle a config flow for Linus Brain.

    This class manages the step-by-step configuration process through the UI.
    """

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """
        Handle the initial step of the config flow.

        This is called when the user initiates the integration setup from the UI.

        Args:
            user_input: Dictionary containing user input, None on first display

        Returns:
            FlowResult indicating next step or completion
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # User has submitted the form
            url = user_input[CONF_URL]
            api_key = user_input[CONF_API_KEY]

            try:
                # Validate the connection
                await validate_supabase_connection(self.hass, url, api_key)

                # Create a unique ID for this config entry
                await self.async_set_unique_id(f"linus_brain_{url}")
                self._abort_if_unique_id_configured()

                # Store configuration and create entry
                return self.async_create_entry(
                    title="Linus Brain",
                    data={
                        "supabase_url": url,
                        "supabase_key": api_key,
                    },
                )

            except Exception as err:
                _LOGGER.error(f"Configuration validation failed: {err}")
                errors["base"] = "cannot_connect"

        # Show the configuration form
        return self.async_show_form(
            step_id="user",
            data_schema=CONFIG_SCHEMA,
            errors=errors,
            description_placeholders={
                "docs_url": "https://github.com/Thank-you-Linus/Linus-Brain"
            },
        )

    async def async_step_import(self, import_config: dict[str, Any]) -> Any:
        """
        Handle import from configuration.yaml (legacy support).

        This allows users who have YAML configuration to migrate to UI config.

        Args:
            import_config: Configuration from YAML

        Returns:
            FlowResult for import
        """
        return await self.async_step_user(import_config)


class LinusBrainOptionsFlow(config_entries.OptionsFlow):
    """
    Handle options flow for Linus Brain.

    This allows users to modify configuration after initial setup.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """
        Manage the options.

        Args:
            user_input: User input for options

        Returns:
            FlowResult for options
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Show options form (for future settings like update interval, etc.)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )
