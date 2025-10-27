"""
Sensor Platform for Linus Brain

This module provides diagnostic sensor entities that expose the integration's
status and statistics in the Home Assistant UI.

Sensor Types:
1. LinusBrainSyncSensor - Last sync time with Supabase
2. LinusBrainRoomsSensor - Number of areas being monitored
3. LinusBrainErrorsSensor - Integration error count
4. LinusAreaContextSensor - Per-area context (activity + environmental state)

Setup Dependencies:
- area_manager: Must be added to hass.data[DOMAIN][entry.entry_id]["area_manager"]
- activity_tracker: Must be added to hass.data[DOMAIN][entry.entry_id]["activity_tracker"]

Area Context Sensors:
- Created for ALL areas with presence detection capability
- Independent from light automation (no light entities required)
- Displays: activity level, illuminance, sun elevation, area state (is_dark/is_bright)
"""

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
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

    sensors = [
        LinusBrainSyncSensor(coordinator, entry),
        LinusBrainRoomsSensor(coordinator, entry),
        LinusBrainErrorsSensor(coordinator, entry),
    ]

    if area_manager and activity_tracker:
        eligible_areas = area_manager.get_activity_tracking_areas()
        for area_id, area_name in eligible_areas.items():
            sensors.append(
                LinusAreaContextSensor(
                    coordinator,
                    area_manager,
                    activity_tracker,
                    area_id,
                    area_name,
                    entry,
                )
            )

    async_add_entities(sensors)
    _LOGGER.info(f"Added {len(sensors)} Linus Brain sensor entities")


class LinusBrainSyncSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing the last sync time with Supabase.
    """

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "Linus Brain Last Sync"
        self._attr_unique_id = f"{DOMAIN}_last_sync"
        self._attr_icon = "mdi:cloud-sync"
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
            self._attr_native_value = self.coordinator.data.get("last_sync")
            self._attr_extra_state_attributes = {
                "sync_count": self.coordinator.data.get("sync_count", 0),
                "areas_synced": self.coordinator.data.get("areas_synced", 0),
                "total_areas": self.coordinator.data.get("total_areas", 0),
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class LinusBrainRoomsSensor(CoordinatorEntity, SensorEntity):
    """
    Sensor showing the number of areas being monitored.
    """

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "Linus Brain Monitored Areas"
        self._attr_unique_id = f"{DOMAIN}_monitored_areas"
        self._attr_icon = "mdi:home-group"
        self._attr_native_unit_of_measurement = "areas"
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

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "Linus Brain Errors"
        self._attr_unique_id = f"{DOMAIN}_errors"
        self._attr_icon = "mdi:alert-circle"
        self._attr_native_unit_of_measurement = "errors"
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

    def __init__(
        self,
        coordinator: LinusBrainCoordinator,
        area_manager: Any,
        activity_tracker: Any,
        area_id: str,
        area_name: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._area_manager = area_manager
        self._activity_tracker = activity_tracker
        self._area_id = area_id
        self._area_name = area_name
        self._attr_name = f"{area_name} Area Context"
        self._attr_unique_id = f"{DOMAIN}_area_context_{area_id}"
        self._attr_icon = "mdi:home-analytics"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["empty", "movement", "occupied", "inactive"]
        self._attr_translation_key = "area_activity"
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
        }
