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
from homeassistant.helpers import area_registry, entity_registry as er

from .const import DOMAIN
from .coordinator import LinusBrainCoordinator
from .services import async_setup_services, async_unload_services
from .utils.event_listener import EventListener
from .utils.insights_manager import InsightsManager
from .utils.light_learning import LightLearning
from .utils.rule_engine import RuleEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Migrate entity IDs from localized names to English.
    
    This function automatically renames entities that were created with localized
    entity_ids (e.g., French) to their proper English equivalents.
    
    This migration is safe to run multiple times - it will only rename entities
    that need renaming.
    
    Args:
        hass: Home Assistant instance
        entry: Config entry for this integration
    """
    entity_reg = er.async_get(hass)
    
    # Get all entities for this integration
    entities = er.async_entries_for_config_entry(entity_reg, entry.entry_id)
    
    if not entities:
        _LOGGER.debug("No entities found for migration check")
        return
    
    # Define the mapping from translation_key to expected English entity_id suffix
    # These are the patterns we expect for properly named entities
    EXPECTED_ENTITY_IDS = {
        # Button entities
        "sync": "sync",
        
        # Sensor entities (global)
        "last_sync": "last_sync",
        "monitored_areas": "monitored_areas",
        "errors": "errors",
        "cloud_health": "cloud_health",
        "rule_engine": "rule_engine",
        "activities": "activities",
        
        # Sensor entities (per-area activity sensors use pattern: activity_{area_id})
        "activity": "activity",
        
        # Sensor entities (per-app sensors use pattern: app_{app_id})
        "app": "app",
        
        # Switch entities (per-area feature switches use pattern: feature_{feature_id}_{area_id})
        "feature_automatic_lighting": "feature_automatic_lighting",
    }
    
    migrations_needed = []
    
    for entity_entry in entities:
        # Skip entities without translation_key
        if not entity_entry.translation_key:
            continue
        
        current_entity_id = entity_entry.entity_id
        platform, current_name = current_entity_id.split(".", 1)
        
        # Determine expected entity_id based on translation_key
        translation_key = entity_entry.translation_key
        
        # Handle different entity types
        if translation_key == "activity":
            # Activity sensors: sensor.linus_brain_activity_{area_id}
            # Extract area_id from unique_id which is: linus_brain_activity_{area_id}
            if entity_entry.unique_id and entity_entry.unique_id.startswith("linus_brain_activity_"):
                area_id = entity_entry.unique_id.replace("linus_brain_activity_", "")
                expected_name = f"linus_brain_activity_{area_id}"
            else:
                continue
                
        elif translation_key == "app":
            # App sensors: sensor.linus_brain_app_{app_id}
            # Extract app_id from unique_id which is: linus_brain_app_{app_id}
            if entity_entry.unique_id and entity_entry.unique_id.startswith("linus_brain_app_"):
                app_id = entity_entry.unique_id.replace("linus_brain_app_", "")
                expected_name = f"linus_brain_app_{app_id}"
            else:
                continue
                
        elif translation_key.startswith("feature_"):
            # Feature switches: switch.linus_brain_feature_{feature_id}_{area_id}
            # Extract from unique_id which is: linus_brain_feature_{feature_id}_{area_id}
            if entity_entry.unique_id and entity_entry.unique_id.startswith("linus_brain_feature_"):
                suffix = entity_entry.unique_id.replace("linus_brain_", "")
                expected_name = f"linus_brain_{suffix}"
            else:
                continue
                
        elif translation_key in EXPECTED_ENTITY_IDS:
            # Standard entities: use direct mapping
            expected_name = f"linus_brain_{EXPECTED_ENTITY_IDS[translation_key]}"
        else:
            # Unknown translation_key, skip
            continue
        
        expected_entity_id = f"{platform}.{expected_name}"
        
        # Check if migration is needed
        if current_entity_id != expected_entity_id:
            migrations_needed.append({
                "entity_entry": entity_entry,
                "current": current_entity_id,
                "expected": expected_entity_id,
                "translation_key": translation_key,
            })
    
    if not migrations_needed:
        _LOGGER.info("Entity ID migration check: All entities already have English entity_ids ✓")
        return
    
    # Perform migrations
    _LOGGER.info(f"Entity ID migration: Found {len(migrations_needed)} entities to migrate")
    
    migrated_count = 0
    for migration in migrations_needed:
        entity_entry = migration["entity_entry"]
        current_id = migration["current"]
        expected_id = migration["expected"]
        
        try:
            # Check if target entity_id already exists
            if entity_reg.async_get(expected_id):
                _LOGGER.warning(
                    f"Cannot migrate {current_id} → {expected_id}: Target entity_id already exists"
                )
                continue
            
            # Perform the migration
            entity_reg.async_update_entity(
                entity_entry.entity_id,
                new_entity_id=expected_id
            )
            
            _LOGGER.info(f"Migrated: {current_id} → {expected_id}")
            migrated_count += 1
            
        except Exception as err:
            _LOGGER.error(
                f"Failed to migrate {current_id} → {expected_id}: {err}"
            )
    
    if migrated_count > 0:
        _LOGGER.info(f"Entity ID migration complete: {migrated_count} entities renamed to English")
    else:
        _LOGGER.warning("Entity ID migration complete: No entities could be migrated")


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

    # Load feature flag states from storage before first refresh
    _LOGGER.info(f"Loading feature flag states for {len(area_ids)} areas")
    await coordinator.feature_flag_manager.load_feature_states(area_ids)

    # Now do the first refresh - ActivityTracker will have activities available
    await coordinator.async_config_entry_first_refresh()

    light_learning = LightLearning(hass, coordinator)

    event_listener = EventListener(hass, coordinator, light_learning)

    await event_listener.async_start_listening()
    entry.async_on_unload(event_listener.async_stop_listening)

    async def async_check_activity_timeouts(_now=None):
        """Check and update activity states based on timeouts."""
        # Check activity states for all areas (activity tracking is always active)
        for area_id in coordinator.activity_tracker._area_states:
            await coordinator.activity_tracker.async_evaluate_activity(area_id)
        coordinator.async_update_listeners()

    from homeassistant.helpers.event import async_track_time_interval
    from datetime import timedelta

    timeout_checker = async_track_time_interval(
        hass, async_check_activity_timeouts, timedelta(seconds=30)
    )
    entry.async_on_unload(timeout_checker)

    async def async_refresh_remote_config(_now=None):
        """Refresh remote configuration (activities, timeouts) from cloud."""
        try:
            _LOGGER.info("Refreshing remote configuration from cloud")
            
            # Refresh activities from Supabase
            activities_updated = await coordinator.app_storage.async_refresh_activities(
                coordinator.supabase_client
            )
            
            if activities_updated:
                # Reload activities in ActivityTracker
                tracker_updated = await coordinator.activity_tracker.async_reload_activities()
                
                if tracker_updated:
                    _LOGGER.info("Remote configuration refreshed successfully")
                    coordinator.async_update_listeners()
                else:
                    _LOGGER.debug("Remote configuration unchanged")
            else:
                _LOGGER.debug("No updates from cloud")
                
        except Exception as err:
            _LOGGER.warning(f"Failed to refresh remote configuration: {err}")
    
    # Refresh remote config every hour
    remote_config_refresher = async_track_time_interval(
        hass, async_refresh_remote_config, timedelta(hours=1)
    )
    entry.async_on_unload(remote_config_refresher)

    rule_engine = RuleEngine(
        hass,
        entry.entry_id,
        coordinator.activity_tracker,
        coordinator.app_storage,
        coordinator.area_manager,
        coordinator.feature_flag_manager,
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

    # Migrate entity IDs from localized names to English (if needed)
    # This runs before platforms are loaded, so it renames existing entities
    # before new ones are created
    await async_migrate_entity_ids(hass, entry)

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
