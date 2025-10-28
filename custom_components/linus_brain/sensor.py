"""
Sensor Platform for Linus Brain

This module provides diagnostic sensor entities that expose the integration's
status and statistics in the Home Assistant UI.

Sensor Types:
1. LinusBrainSyncSensor - Last sync time with Supabase (DIAGNOSTIC)
2. LinusBrainRoomsSensor - Number of areas being monitored (DIAGNOSTIC)
3. LinusBrainErrorsSensor - Integration error count (DIAGNOSTIC)
4. LinusBrainCloudHealthSensor - Cloud sync health status (DIAGNOSTIC)
5. LinusBrainRuleEngineStatsSensor - Rule engine performance and statistics (DIAGNOSTIC)
6. LinusAreaContextSensor - Per-area context (DIAGNOSTIC)
7. LinusBrainActivitiesSensor - Activities catalog from Supabase (DIAGNOSTIC)
8. LinusBrainAppSensor - Per-app details with version and actions (DIAGNOSTIC)

Setup Dependencies:
- area_manager: Must be added to hass.data[DOMAIN][entry.entry_id]["area_manager"]
- activity_tracker: Must be added to hass.data[DOMAIN][entry.entry_id]["activity_tracker"]
- rule_engine: Optional, for rule engine stats sensor
- app_storage: Required for activities and apps sensors

Area Context Sensors:
- Created for ALL areas with presence detection capability
- Independent from light automation (no light entities required)
- Displays: activity level, illuminance, sun elevation, area state (is_dark/is_bright)
"""

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LinusBrainCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up Linus Brain sensor entities from a config entry.

    Args:
        hass: Home Assistant instance
        entry: Config entry
        async_add_entities: Callback to add entities
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    area_manager = hass.data[DOMAIN][entry.entry_id].get("area_manager")
    activity_tracker = hass.data[DOMAIN][entry.entry_id].get("activity_tracker")
    rule_engine = hass.data[DOMAIN][entry.entry_id].get("rule_engine")
    insights_manager = hass.data[DOMAIN][entry.entry_id].get("insights_manager")

    sensors = [
        LinusBrainSyncSensor(coordinator, entry),
        LinusBrainRoomsSensor(coordinator, entry),
        LinusBrainErrorsSensor(coordinator, entry),
        LinusBrainCloudHealthSensor(coordinator, entry),
    ]

    # Add activities catalog sensor
    _LOGGER.info("Creating activities catalog sensor")
    sensors.append(LinusBrainActivitiesSensor(coordinator, entry))

    # Add per-app sensors
    apps = coordinator.app_storage.get_apps()
    _LOGGER.info(f"Creating app sensors for {len(apps)} apps: {list(apps.keys())}")
    for app_id, app_data in apps.items():
        _LOGGER.info(f"Creating sensor for app: {app_id}")
        sensors.append(LinusBrainAppSensor(coordinator, app_id, app_data, entry))

    # Add rule engine stats sensor if available
    if rule_engine:
        sensors.append(LinusBrainRuleEngineStatsSensor(coordinator, rule_engine, entry))

    if area_manager and activity_tracker:
        eligible_areas = area_manager.get_activity_tracking_areas()
        for area_id, area_name in eligible_areas.items():
            sensors.append(
                LinusAreaContextSensor(
                    coordinator,
                    area_manager,
                    activity_tracker,
                    insights_manager,
                    area_id,
                    area_name,
                    entry,
                )
            )

    async_add_entities(sensors)
    _LOGGER.info(f"Added {len(sensors)} Linus Brain sensor entities")


class LinusBrainSyncSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing the last cloud sync time from Supabase.

    This tracks real cloud synchronization of apps/activities/assignments,
    not the local event-driven activity updates.
    """

    coordinator: LinusBrainCoordinator

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "last_sync"
        self._attr_unique_id = f"{DOMAIN}_last_sync"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:cloud-sync"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from coordinator data."""
        # Get real cloud sync time from app_storage
        sync_time = self.coordinator.app_storage.get_sync_time()  # type: ignore[attr-defined]

        if sync_time:
            self._attr_native_value = sync_time.isoformat()

            # Get storage stats
            activities = self.coordinator.app_storage.get_activities()  # type: ignore[attr-defined]
            apps = self.coordinator.app_storage.get_apps()  # type: ignore[attr-defined]
            assignments = self.coordinator.app_storage.get_assignments()  # type: ignore[attr-defined]
            is_fallback = self.coordinator.app_storage.is_fallback_data()  # type: ignore[attr-defined]

            self._attr_extra_state_attributes = {
                "activities_loaded": len(activities),
                "apps_loaded": len(apps),
                "assignments_loaded": len(assignments),
                "is_fallback_data": is_fallback,
                "supabase_url": self.coordinator.supabase_url,  # type: ignore[attr-defined]
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {
                "status": "Never synced",
                "is_fallback_data": self.coordinator.app_storage.is_fallback_data(),  # type: ignore[attr-defined]
            }


class LinusBrainRoomsSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing the number of areas being monitored.
    """

    coordinator: LinusBrainCoordinator

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "monitored_areas"
        self._attr_unique_id = f"{DOMAIN}_monitored_areas"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:home-group"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from coordinator data."""
        if self.coordinator.data:
            self._attr_native_value = self.coordinator.data.get("total_areas", 0)

            area_states = self.coordinator.data.get("area_states", [])
            occupied_areas = sum(
                1 for area in area_states if area.get("presence_detected", False)
            )

            self._attr_extra_state_attributes = {
                "occupied_areas": occupied_areas,
                "areas": [area.get("area") for area in area_states],
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class LinusBrainErrorsSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing the error count for the integration.
    """

    coordinator: LinusBrainCoordinator

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "errors"
        self._attr_unique_id = f"{DOMAIN}_errors"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:alert-circle"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from coordinator data."""
        if self.coordinator.data:
            self._attr_native_value = self.coordinator.data.get("error_count", 0)
        else:
            self._attr_native_value = getattr(self.coordinator, "error_count", 0)

        total_syncs = getattr(self.coordinator, "sync_count", 0)
        errors = getattr(self.coordinator, "error_count", 0)

        if total_syncs > 0:
            success_rate = ((total_syncs - errors) / total_syncs) * 100
        else:
            success_rate = 100

        self._attr_extra_state_attributes = {
            "total_syncs": total_syncs,
            "success_rate": round(success_rate, 1),
            "supabase_url": getattr(self.coordinator, "supabase_url", "Unknown"),
        }


class LinusAreaContextSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing area context (activity + environmental state) for a specific area.
    """

    coordinator: LinusBrainCoordinator

    def __init__(
        self,
        coordinator: LinusBrainCoordinator,
        area_manager: Any,
        activity_tracker: Any,
        insights_manager: Any,
        area_id: str,
        area_name: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._area_manager = area_manager
        self._activity_tracker = activity_tracker
        self._insights_manager = insights_manager
        self._area_id = area_id
        self._area_name = area_name
        self._attr_unique_id = f"{DOMAIN}_activity_{area_id}"
        self._attr_translation_key = "activity"
        self._attr_has_entity_name = True
        self._attr_translation_placeholders = {"area_name": area_name}
        self._attr_icon = "mdi:home-analytics"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["empty", "movement", "occupied", "inactive"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_activity_tracker()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_activity_tracker()
        super()._handle_coordinator_update()

    def _update_from_activity_tracker(self) -> None:
        """Update sensor attributes from activity tracker and area manager."""
        activity_level = self._activity_tracker.get_activity(self._area_id)
        self._attr_native_value = activity_level
        _LOGGER.debug(f"Sensor update for {self._area_name}: activity={activity_level}")

        time_until_state_loss = self._activity_tracker.get_time_until_state_loss(
            self._area_id
        )
        area_state = self._area_manager.get_area_environmental_state(self._area_id)
        tracking_entities = self._area_manager.get_tracking_entities(self._area_id)
        last_rule = (
            self.coordinator.data.get("last_rules", {}).get(self._area_id)
            if self.coordinator.data
            else None
        )
        active_presence_entities = self._coordinator.active_presence_entities.get(
            self._area_id, []
        )

        seconds_until_timeout = None
        if time_until_state_loss is not None:
            seconds_until_timeout = round(time_until_state_loss, 1)

        # Get insights for this area
        insights = {}
        if self._insights_manager and self._coordinator.instance_id:
            insights = self._insights_manager.get_all_insights_for_area(
                self._coordinator.instance_id, self._area_id
            )

        self._attr_extra_state_attributes = {
            "activity_level": activity_level,
            "seconds_until_timeout": seconds_until_timeout,
            "active_presence_entities": active_presence_entities,
            "illuminance": area_state.get("illuminance"),
            "temperature": area_state.get("temperature"),
            "humidity": area_state.get("humidity"),
            "sun_elevation": area_state.get("sun_elevation"),
            "is_dark": area_state.get("is_dark"),
            "tracking_entities": tracking_entities,
            "last_automation_rule": last_rule,
            "insights": insights,
        }


class LinusBrainRuleEngineStatsSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing rule engine statistics and performance.
    """

    coordinator: LinusBrainCoordinator

    def __init__(
        self, coordinator: LinusBrainCoordinator, rule_engine: Any, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._rule_engine = rule_engine
        self._attr_translation_key = "rule_engine"
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{DOMAIN}_rule_engine"
        self._attr_icon = "mdi:robot"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_rule_engine()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_rule_engine()
        super()._handle_coordinator_update()

    def _update_from_rule_engine(self) -> None:
        """Update sensor attributes from rule engine stats."""
        stats = self._rule_engine.get_stats()

        total_triggers = stats.get("total_triggers", 0)
        successful = stats.get("successful_executions", 0)
        failed = stats.get("failed_executions", 0)
        cooldown_blocks = stats.get("cooldown_blocks", 0)

        # Calculate success rate
        total_executions = successful + failed
        if total_executions > 0:
            success_rate = round((successful / total_executions) * 100, 1)
        else:
            success_rate = 100.0

        self._attr_native_value = total_triggers

        # Get enabled areas list
        enabled_areas = list(self._rule_engine._enabled_areas)

        self._attr_extra_state_attributes = {
            "total_triggers": total_triggers,
            "successful_executions": successful,
            "failed_executions": failed,
            "cooldown_blocks": cooldown_blocks,
            "success_rate": success_rate,
            "enabled_areas_count": len(enabled_areas),
            "enabled_areas": enabled_areas,
            "total_assignments": stats.get("total_assignments", 0),
        }


class LinusBrainCloudHealthSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing cloud sync health and connection status.
    """

    coordinator: LinusBrainCoordinator

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "cloud_health"
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{DOMAIN}_cloud_health"
        self._attr_icon = "mdi:cloud-check"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["connected", "disconnected", "error"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from coordinator health data."""
        # Determine health status based on errors and sync success
        error_count = self.coordinator.error_count
        sync_count = self.coordinator.sync_count
        last_sync = self.coordinator.last_sync_time

        # Determine status
        if sync_count == 0:
            status = "disconnected"
        elif error_count > 0 and sync_count > 0:
            error_rate = error_count / sync_count
            if error_rate > 0.5:
                status = "error"
            elif error_rate > 0.1:
                status = "disconnected"
            else:
                status = "connected"
        else:
            status = "connected"

        self._attr_native_value = status

        # Change icon based on status
        if status == "connected":
            self._attr_icon = "mdi:cloud-check"
        elif status == "disconnected":
            self._attr_icon = "mdi:cloud-off-outline"
        else:
            self._attr_icon = "mdi:cloud-alert"

        # Get apps and activities loaded
        apps_loaded = len(self.coordinator.app_storage.get_apps())
        activities_loaded = len(self.coordinator.app_storage.get_activities())

        self._attr_extra_state_attributes = {
            "status": status,
            "last_successful_sync": last_sync,
            "total_syncs": sync_count,
            "total_errors": error_count,
            "instance_id": self.coordinator.instance_id,
            "apps_loaded": apps_loaded,
            "activities_loaded": activities_loaded,
            "supabase_url": self.coordinator.supabase_url,
        }


class LinusBrainActivitiesSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing all available activity types from Supabase.

    Displays the activities catalog that can be used in automation rules.
    """

    coordinator: LinusBrainCoordinator

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "activities"
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{DOMAIN}_activities"
        self._attr_icon = "mdi:run"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from app_storage."""
        activities = self.coordinator.app_storage.get_activities()  # type: ignore[attr-defined]
        is_fallback = self.coordinator.app_storage.is_fallback_data()  # type: ignore[attr-defined]
        sync_time = self.coordinator.app_storage.get_sync_time()  # type: ignore[attr-defined]

        self._attr_native_value = len(activities)

        self._attr_extra_state_attributes = {
            "activities": activities,
            "activity_ids": list(activities.keys()),
            "is_fallback": is_fallback,
            "synced_at": sync_time.isoformat() if sync_time else None,
        }


class LinusBrainAppSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing details for a specific app.

    Displays version, actions, and areas using this app.
    """

    coordinator: LinusBrainCoordinator

    def __init__(
        self,
        coordinator: LinusBrainCoordinator,
        app_id: str,
        app_data: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._app_id = app_id
        self._app_name = app_data.get("name", app_id.title())
        self._attr_translation_key = "app"
        self._attr_has_entity_name = True
        self._attr_translation_placeholders = {"app_name": self._app_name}
        self._attr_unique_id = f"{DOMAIN}_app_{app_id}"
        self._attr_icon = "mdi:application-cog"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }
        self._update_from_coordinator()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Update sensor attributes from app_storage."""
        app_data = self.coordinator.app_storage.get_app(self._app_id)  # type: ignore[attr-defined]

        if not app_data:
            self._attr_native_value = "not_found"
            self._attr_extra_state_attributes = {"error": "App not found in storage"}
            return

        # Get version
        version = app_data.get("version", "default")
        self._attr_native_value = version

        # Get activity actions
        activity_actions = app_data.get("activity_actions", {})
        supported_activities = list(activity_actions.keys())
        total_actions = sum(len(actions) for actions in activity_actions.values())

        # Get areas using this app
        assignments = self.coordinator.app_storage.get_assignments()  # type: ignore[attr-defined]
        areas_using_app = [
            area_id
            for area_id, assignment in assignments.items()
            if assignment.get("app_id") == self._app_id
        ]

        self._attr_extra_state_attributes = {
            "app_id": self._app_id,
            "app_name": self._app_name,
            "version": version,
            "description": app_data.get("description", ""),
            "created_by": app_data.get("created_by", "unknown"),
            "activity_actions": activity_actions,
            "supported_activities": supported_activities,
            "total_actions": total_actions,
            "areas_assigned": areas_using_app,
            "areas_count": len(areas_using_app),
        }
