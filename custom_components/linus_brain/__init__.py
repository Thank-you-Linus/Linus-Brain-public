"""
Linus Brain - Home Assistant Custom Integration

This component serves as an AI bridge between Home Assistant and a cloud brain (Supabase).
It learns presence patterns per area by collecting local signals and can later return
automation rules based on AI analysis.

Main responsibilities:
- Initialize the integration with Supabase credentials
- Set up the data coordinator for state management
- Register event listeners for entity state changes
- Load diagnostic sensor platforms
- Manage integration lifecycle (setup/unload)
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import area_registry

from .const import DOMAIN
from .coordinator import LinusBrainCoordinator
from .services import async_setup_services, async_unload_services
from .utils.event_listener import EventListener
from .utils.insights_manager import InsightsManager
from .utils.light_learning import LightLearning
from .utils.rule_engine import RuleEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up Linus Brain from a config entry.

    This function is called when the user has configured the integration via the UI.
    It initializes the coordinator, event listener, and loads platforms.

    Args:
        hass: Home Assistant instance
        entry: Config entry containing Supabase URL and API key

    Returns:
        True if setup was successful, False otherwise
    """
    _LOGGER.info("Setting up Linus Brain integration")

    # Retrieve configuration from the config entry
    supabase_url = entry.data.get("supabase_url")
    supabase_key = entry.data.get("supabase_key")

    if not supabase_url or not supabase_key:
        _LOGGER.error("Missing Supabase URL or API key in configuration")
        raise ConfigEntryNotReady("Missing Supabase credentials")

    # Initialize the data coordinator
    # This manages periodic updates and state aggregation
    coordinator = LinusBrainCoordinator(
        hass=hass,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        config_entry=entry,
    )

    # Initialize app storage with cloud sync BEFORE first refresh
    # This ensures ActivityTracker has activities available when it initializes
    instance_id = await coordinator.get_or_create_instance_id()
    area_ids = [area.id for area in area_registry.async_get(hass).async_list_areas()]

    _LOGGER.info(
        f"Initializing app storage for instance {instance_id} with {len(area_ids)} areas"
    )
    await coordinator.app_storage.async_initialize(
        coordinator.supabase_client, instance_id, area_ids
    )

    # Initialize insights manager and load insights from Supabase
    insights_manager = InsightsManager(coordinator.supabase_client)
    await insights_manager.async_load(instance_id)
    _LOGGER.info(f"Loaded {len(insights_manager._cache)} insights from Supabase")

    # Pass insights_manager to area_manager for AI-learned thresholds
    coordinator.area_manager._insights_manager = insights_manager

    # Now do the first refresh - ActivityTracker will have activities available
    await coordinator.async_config_entry_first_refresh()

    light_learning = LightLearning(hass, coordinator)

    event_listener = EventListener(hass, coordinator, light_learning)

    await event_listener.async_start_listening()
    entry.async_on_unload(event_listener.async_stop_listening)

    async def async_check_activity_timeouts(_now=None):
        """Check and update activity states based on timeouts."""
        for area_id in coordinator.activity_tracker._area_states:
            await coordinator.activity_tracker.async_evaluate_activity(area_id)
        coordinator.async_update_listeners()

    from homeassistant.helpers.event import async_track_time_interval
    from datetime import timedelta

    timeout_checker = async_track_time_interval(
        hass, async_check_activity_timeouts, timedelta(seconds=30)
    )
    entry.async_on_unload(timeout_checker)

    rule_engine = RuleEngine(
        hass,
        entry.entry_id,
        coordinator.activity_tracker,
        coordinator.app_storage,
        coordinator.area_manager,
    )
    await rule_engine.async_initialize()
    entry.async_on_unload(rule_engine.async_shutdown)

    # Link rule_engine to coordinator for activity-triggered evaluations
    coordinator.rule_engine = rule_engine

    # Link coordinator to activity_tracker for timeout-triggered updates
    coordinator.activity_tracker.coordinator = coordinator

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "event_listener": event_listener,
        "light_learning": light_learning,
        "rule_engine": rule_engine,
        "area_manager": coordinator.area_manager,
        "activity_tracker": coordinator.activity_tracker,
        "insights_manager": insights_manager,
    }

    # Register services (only once, not per config entry)
    if len(hass.data[DOMAIN]) == 1:
        await async_setup_services(hass)
        entry.async_on_unload(lambda: async_unload_services(hass))

    # Forward the setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    _LOGGER.info("Linus Brain integration setup completed successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Unload a config entry.

    This function is called when the integration is being removed or reloaded.
    It ensures proper cleanup of listeners and resources.

    Args:
        hass: Home Assistant instance
        entry: Config entry to unload

    Returns:
        True if unload was successful
    """
    _LOGGER.info("Unloading Linus Brain integration")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN][entry.entry_id]

        event_listener = entry_data["event_listener"]
        await event_listener.async_stop_listening()

        rule_engine = entry_data.get("rule_engine")
        if rule_engine:
            await rule_engine.async_shutdown()

        hass.data[DOMAIN].pop(entry.entry_id)

        # Unload services if this was the last config entry
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)

        _LOGGER.info("Linus Brain integration unloaded successfully")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Reload config entry.

    Called when the user updates the configuration.

    Args:
        hass: Home Assistant instance
        entry: Config entry to reload
    """
    _LOGGER.info("Reloading Linus Brain integration")
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
