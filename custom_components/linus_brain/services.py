"""
Services for Linus Brain integration.

This module defines custom services that users can call to interact with
the Linus Brain integration programmatically.
"""

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_SYNC_NOW = "sync_now"
SERVICE_FETCH_RULES = "fetch_rules"
SERVICE_SEND_AREA_UPDATE = "send_area_update"
SERVICE_RELOAD_RULES = "reload_rules"
SERVICE_SIMULATE_ACTIVITY = "simulate_activity"
SERVICE_LOAD_RULE_FROM_CLOUD = "load_rule_from_cloud"
SERVICE_RELOAD_APPS = "reload_apps"

# Service schemas
SERVICE_SEND_AREA_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required("area"): cv.string,
    }
)

SERVICE_SIMULATE_ACTIVITY_SCHEMA = vol.Schema(
    {
        vol.Required("area_id"): cv.string,
        vol.Required("activity"): vol.In(["none", "presence", "occupation"]),
        vol.Optional("duration", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=3600)
        ),
    }
)

SERVICE_LOAD_RULE_FROM_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required("area_id"): cv.string,
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """
    Set up services for Linus Brain.

    Args:
        hass: Home Assistant instance
    """

    async def handle_sync_now(call: ServiceCall) -> None:
        """
        Handle the sync_now service call.

        Forces an immediate sync of all area states to Supabase.
        """
        _LOGGER.info("Service sync_now called")

        # Get all coordinators for all config entries
        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coordinator = entry_data.get("coordinator")
            if coordinator:
                await coordinator.async_refresh()
                _LOGGER.info(f"Forced sync for entry {entry_id}")

    async def handle_fetch_rules(call: ServiceCall) -> None:
        """
        Handle the fetch_rules service call.

        Fetches automation rules from Supabase and syncs to local storage.
        """
        _LOGGER.info("Service fetch_rules called")

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coordinator = entry_data.get("coordinator")
            rule_engine = entry_data.get("rule_engine")
            switches = entry_data.get("switches", {})
            if coordinator and rule_engine:
                await coordinator.async_fetch_and_sync_rules()
                count = await rule_engine.reload_rules()

                for area_id, switch in switches.items():
                    rule = await rule_engine.get_rule(area_id)
                    if rule and hasattr(switch, "update_rule_data"):
                        switch.update_rule_data(rule)

                _LOGGER.info(f"Fetched and reloaded {count} rules for entry {entry_id}")

    async def handle_send_area_update(call: ServiceCall) -> None:
        """
        Handle the send_area_update service call.

        Sends an immediate update for a specific area.
        """
        area = call.data.get("area")
        _LOGGER.info(f"Service send_area_update called for area: {area}")

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coordinator = entry_data.get("coordinator")
            if coordinator:
                await coordinator.async_send_area_update(area)
                _LOGGER.info(f"Sent update for area {area} (entry {entry_id})")

    async def handle_reload_rules(call: ServiceCall) -> None:
        """
        Handle the reload_rules service call.

        Reloads automation rules from local storage.
        """
        _LOGGER.info("Service reload_rules called")

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            rule_engine = entry_data.get("rule_engine")
            switches = entry_data.get("switches", {})
            if rule_engine:
                count = await rule_engine.reload_rules()

                for area_id, switch in switches.items():
                    rule = await rule_engine.get_rule(area_id)
                    if rule and hasattr(switch, "update_rule_data"):
                        switch.update_rule_data(rule)

                _LOGGER.info(f"Reloaded {count} rules for entry {entry_id}")

    async def handle_simulate_activity(call: ServiceCall) -> None:
        """
        Handle the simulate_activity service call.

        Simulates activity for an area and triggers rule evaluation.
        """
        area_id = call.data.get("area_id")
        activity = call.data.get("activity")
        duration = call.data.get("duration", 0)

        _LOGGER.info(
            f"Service simulate_activity called: area={area_id}, activity={activity}, duration={duration}s"
        )

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            activity_tracker = entry_data.get("activity_tracker")
            rule_engine = entry_data.get("rule_engine")

            if activity_tracker and rule_engine:
                await activity_tracker.simulate_activity(area_id, activity, duration)

                if activity != "none":
                    await rule_engine._async_evaluate_and_execute(area_id)
                    _LOGGER.info(f"Triggered rule evaluation for area {area_id}")

    async def handle_load_rule_from_cloud(call: ServiceCall) -> None:
        """
        Handle the load_rule_from_cloud service call.

        Fetches a specific area rule from Supabase and loads it.
        """
        area_id = call.data.get("area_id")
        _LOGGER.info(f"Service load_rule_from_cloud called for area: {area_id}")

        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coordinator = entry_data.get("coordinator")
            rule_engine = entry_data.get("rule_engine")
            switches = entry_data.get("switches", {})

            if not coordinator or not rule_engine:
                continue

            supabase_client = coordinator.supabase_client
            instance_id = coordinator.instance_id
            local_storage = rule_engine.local_storage

            try:
                rule = await supabase_client.get_rule_for_area(instance_id, area_id)

                if not rule:
                    _LOGGER.warning(f"No rule found in cloud for area {area_id}")
                    continue

                await local_storage.save_rule(area_id, rule)
                _LOGGER.info(f"Saved rule for area {area_id} to local storage")

                await rule_engine.reload_rules()

                if area_id in switches:
                    switch = switches[area_id]
                    if hasattr(switch, "update_rule_data"):
                        switch.update_rule_data(rule)
                    _LOGGER.info(f"Updated switch display for area {area_id}")

                _LOGGER.info(f"Successfully loaded rule for area {area_id} from cloud")

            except Exception as err:
                _LOGGER.error(f"Failed to load rule for area {area_id}: {err}")

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_NOW,
        handle_sync_now,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_RULES,
        handle_fetch_rules,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_AREA_UPDATE,
        handle_send_area_update,
        schema=SERVICE_SEND_AREA_UPDATE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RELOAD_RULES,
        handle_reload_rules,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SIMULATE_ACTIVITY,
        handle_simulate_activity,
        schema=SERVICE_SIMULATE_ACTIVITY_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOAD_RULE_FROM_CLOUD,
        handle_load_rule_from_cloud,
        schema=SERVICE_LOAD_RULE_FROM_CLOUD_SCHEMA,
    )

    _LOGGER.info("Linus Brain services registered")


async def async_unload_services(hass: HomeAssistant) -> None:
    """
    Unload services for Linus Brain.

    Args:
        hass: Home Assistant instance
    """
    hass.services.async_remove(DOMAIN, SERVICE_SYNC_NOW)
    hass.services.async_remove(DOMAIN, SERVICE_FETCH_RULES)
    hass.services.async_remove(DOMAIN, SERVICE_SEND_AREA_UPDATE)
    hass.services.async_remove(DOMAIN, SERVICE_RELOAD_RULES)
    hass.services.async_remove(DOMAIN, SERVICE_SIMULATE_ACTIVITY)
    hass.services.async_remove(DOMAIN, SERVICE_LOAD_RULE_FROM_CLOUD)

    _LOGGER.info("Linus Brain services unregistered")
