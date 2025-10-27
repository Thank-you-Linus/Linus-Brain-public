"""
Area Manager for Linus Brain

This module handles the grouping of entities by area and computes
binary presence detection based on multiple sensor inputs.

Key responsibilities:
- Discover entities by domain and device class
- Group entities by area_id
- Compute binary presence detection
- Generate JSON payloads for Supabase
"""

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, State, split_entity_id
from homeassistant.helpers import area_registry, device_registry, entity_registry

from ..const import MONITORED_DOMAINS, PRESENCE_DETECTION_DOMAINS

_LOGGER = logging.getLogger(__name__)


class AreaManager:
    """
    Manages area-based entity grouping and binary presence detection.

    This class is responsible for:
    - Identifying relevant entities in each area
    - Reading their current states
    - Computing binary presence detection for each area
    - Formatting data for transmission to Supabase
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """
        Initialize the area manager.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self._entity_registry = entity_registry.async_get(hass)
        self._area_registry = area_registry.async_get(hass)

    def _get_monitored_entities(self) -> dict[str, list[str]]:
        """
        Get all entities that should be monitored, grouped by area.

        Returns:
            Dictionary mapping area_id to list of entity_ids
        """
        area_entities: dict[str, list[str]] = {}

        # Iterate through all registered entities
        for entity in self._entity_registry.entities.values():
            # Check if entity is in a monitored domain
            domain = entity.domain

            if domain not in MONITORED_DOMAINS:
                continue

            # Check device class (if applicable)
            device_classes = MONITORED_DOMAINS[domain]
            if device_classes and entity.original_device_class not in device_classes:
                continue

            # Get the area for this entity
            area_id = entity.area_id

            # If entity doesn't have an area, try to get it from device
            if not area_id and entity.device_id:
                device_registry_instance = device_registry.async_get(self.hass)
                device = device_registry_instance.async_get(entity.device_id)
                if device:
                    area_id = device.area_id

            # Skip entities without an area
            if not area_id:
                continue

            # Add to area's entity list
            if area_id not in area_entities:
                area_entities[area_id] = []
            area_entities[area_id].append(entity.entity_id)

        _LOGGER.debug(f"Found monitored entities in {len(area_entities)} areas")
        return area_entities

    def _get_entity_state(self, entity_id: str) -> State | None:
        """
        Get the current state of an entity.

        Args:
            entity_id: The entity ID

        Returns:
            State object or None if entity doesn't exist
        """
        return self.hass.states.get(entity_id)

    @staticmethod
    def _get_device_class(state: State) -> str | None:
        """
        Get device class from entity state, trying both original_device_class and device_class.

        In Home Assistant, device_class can be in different attributes depending on timing:
        - original_device_class: Set during entity initialization
        - device_class: May not be available immediately after startup

        Args:
            state: Entity state object

        Returns:
            Device class string or None
        """
        return state.attributes.get("original_device_class") or state.attributes.get(
            "device_class"
        )

    def _compute_presence_detected(self, entity_states: dict[str, Any]) -> bool:
        """
        Compute binary presence detection based on entity states.

        Presence is detected if ANY of the following are active:
        - Motion sensor is "on"
        - Presence sensor is "on"
        - Occupancy sensor is "on"
        - Media player is "playing" or "on"

        Args:
            entity_states: Dictionary of entity types to their values

        Returns:
            True if presence detected, False otherwise
        """
        return (
            entity_states.get("motion") == "on"
            or entity_states.get("presence") == "on"
            or entity_states.get("occupancy") == "on"
            or entity_states.get("media") in ["playing", "on"]
        )

    async def get_area_state(self, area_id: str) -> dict[str, Any] | None:
        """
        Get the current state for a specific area.

        Args:
            area_id: The area ID

        Returns:
            Dictionary containing area data, or None if no data
        """
        # Get area name
        area = self._area_registry.async_get_area(area_id)
        if not area:
            return None

        area_name = area.name

        # Get entities in this area
        area_entities_map = self._get_monitored_entities()
        entity_ids = area_entities_map.get(area_id, [])

        if not entity_ids:
            return None

        # Collect entity states
        entity_states: dict[str, Any] = {}
        active_presence_entities: list[str] = []

        for entity_id in entity_ids:
            state = self._get_entity_state(entity_id)
            if not state:
                continue

            domain = split_entity_id(entity_id)[0]

            # Binary sensors (motion, presence, occupancy)
            if domain == "binary_sensor":
                device_class = self._get_device_class(state)
                if device_class in ["motion", "presence", "occupancy"]:
                    entity_states[device_class] = state.state
                    if state.state == "on":
                        active_presence_entities.append(entity_id)

            # Illuminance sensors
            elif domain == "sensor":
                device_class = self._get_device_class(state)
                if device_class == "illuminance":
                    try:
                        entity_states["luminosity"] = float(state.state)
                    except (ValueError, TypeError):
                        pass

            # Media players
            elif domain == "media_player":
                entity_states["media"] = state.state
                if state.state in ["playing", "on"]:
                    active_presence_entities.append(entity_id)

        # Compute binary presence detection
        presence_detected = self._compute_presence_detected(entity_states)

        # Build payload
        payload = {
            "area_id": area_id,
            "area_name": area_name,
            "timestamp": datetime.now().astimezone().isoformat(),
            "entities": {
                "motion": entity_states.get("motion", "off"),
                "presence": entity_states.get("presence", "off"),
                "occupancy": entity_states.get("occupancy", "off"),
                "media": entity_states.get("media", "off"),
                "luminosity": entity_states.get("luminosity", 0.0),
            },
            "presence_detected": presence_detected,
            "active_presence_entities": active_presence_entities,
        }

        return payload

    async def get_all_area_states(self) -> list[dict[str, Any]]:
        """
        Get current states for all areas with monitored entities.

        Returns:
            List of area state dictionaries
        """
        area_entities_map = self._get_monitored_entities()
        area_states = []

        for area_id in area_entities_map.keys():
            area_data = await self.get_area_state(area_id)
            if area_data:
                area_states.append(area_data)

        return area_states

    def get_all_areas(self) -> dict[str, str]:
        """
        Get all areas with monitored entities.

        Returns:
            Dictionary mapping area_id to area_name
        """
        area_entities_map = self._get_monitored_entities()
        areas = {}

        for area_id in area_entities_map.keys():
            area = self._area_registry.async_get_area(area_id)
            if area:
                areas[area_id] = area.name

        return areas

    def _get_entity_area_id(self, entity: entity_registry.RegistryEntry) -> str | None:
        """
        Get the area ID for an entity, checking device if entity has no area.

        Args:
            entity: Entity registry entry

        Returns:
            Area ID or None if not found
        """
        if entity.area_id:
            return entity.area_id

        if entity.device_id:
            device_registry_instance = device_registry.async_get(self.hass)
            device = device_registry_instance.async_get(entity.device_id)
            if device and device.area_id:
                return device.area_id

        return None

    def _has_entities_in_area(
        self, area_id: str, domain: str, device_class: str | None = None
    ) -> bool:
        """
        Check if area has entities matching domain and optional device class.

        Args:
            area_id: Area ID to check
            domain: Entity domain (e.g., "light", "binary_sensor")
            device_class: Optional device class filter

        Returns:
            True if matching entities found in area
        """
        _LOGGER.debug(
            f"Checking for entities in area {area_id} with domain {domain} and device_class {device_class}"
        )
        entities = self._entity_registry.entities.values()
        _LOGGER.debug(f"Entities in registry: {entities}")
        for entity in entities:
            entity_area_id = self._get_entity_area_id(entity)

            if entity_area_id != area_id:
                continue

            entity_domain = entity.domain
            if entity_domain != domain:
                continue

            if device_class is not None:
                entity_device_class = (
                    entity.original_device_class or entity.device_class
                )
                if entity_device_class != device_class:
                    continue

            return True

        return False

    def has_presence_detection(
        self,
        area_id: str,
        presence_config: dict[str, list[str]] | None = None,
    ) -> bool:
        """
        Check if area has presence detection capabilities.

        Uses PRESENCE_DETECTION_DOMAINS from const.py by default, or custom config.

        Args:
            area_id: Area ID to check
            presence_config: Optional custom presence detection config
                           Format: {"domain": ["device_class1", "device_class2"]}
                           Example: {"binary_sensor": ["motion", "presence"]}

        Returns:
            True if area has at least one presence detection entity
        """
        config = presence_config or PRESENCE_DETECTION_DOMAINS

        for domain, device_classes in config.items():
            if not device_classes:
                if self._has_entities_in_area(area_id, domain):
                    return True
            else:
                for device_class in device_classes:
                    if self._has_entities_in_area(area_id, domain, device_class):
                        return True

        return False

    def get_activity_tracking_areas(self) -> dict[str, str]:
        """
        Get areas with activity tracking capability (presence detection).

        Returns areas that have presence detection entities, regardless of
        whether they have lights or other automation prerequisites.
        Used for creating diagnostic sensors that display area context.

        Returns:
            Dictionary mapping area_id to area_name for areas with presence detection
        """
        eligible_areas = {}

        for area in self._area_registry.async_list_areas():
            if self.has_presence_detection(area.id):
                eligible_areas[area.id] = area.name

        _LOGGER.debug(
            f"Found {len(eligible_areas)} areas with activity tracking capability"
        )
        return eligible_areas

    def get_light_automation_eligible_areas(self) -> dict[str, str]:
        """
        Get areas eligible for light automation switches.

        An area is eligible if it has:
        - At least one light entity
        - At least one presence detection entity (configured in PRESENCE_DETECTION_DOMAINS)

        Returns:
            Dictionary mapping area_id to area_name for eligible areas
        """
        eligible_areas = {}

        for area in self._area_registry.async_list_areas():
            area_id = area.id

            has_light = self._has_entities_in_area(area_id, "light")
            if not has_light:
                continue

            if self.has_presence_detection(area_id):
                eligible_areas[area_id] = area.name

        _LOGGER.debug(
            f"Found {len(eligible_areas)} areas eligible for light automation"
        )
        return eligible_areas

    def get_entity_area(self, entity_id: str) -> str | None:
        """
        Get the area ID for a specific entity.

        Args:
            entity_id: The entity ID to look up

        Returns:
            Area ID or None if not found
        """
        entity = self._entity_registry.async_get(entity_id)
        if not entity:
            return None

        area_id = entity.area_id

        # Try to get from device if entity doesn't have area
        if not area_id and entity.device_id:
            device_registry_instance = device_registry.async_get(self.hass)
            device = device_registry_instance.async_get(entity.device_id)
            if device:
                area_id = device.area_id

        return area_id

    def get_area_presence_binary(
        self,
        area_id: str,
        presence_config: dict[str, list[str]] | None = None,
    ) -> bool:
        """
        Get binary presence detection for an area.

        Checks if any presence detection entities are currently active (on).
        Uses PRESENCE_DETECTION_DOMAINS from const.py by default.

        Args:
            area_id: The area ID to check
            presence_config: Optional custom presence detection config
                           Format: {"domain": ["device_class1", "device_class2"]}

        Returns:
            True if presence detected, False otherwise
        """
        config = presence_config or PRESENCE_DETECTION_DOMAINS

        for entity in self._entity_registry.entities.values():
            entity_area_id = self._get_entity_area_id(entity)

            if entity_area_id != area_id:
                continue

            domain = entity.domain
            if domain not in config:
                continue

            device_classes = config[domain]
            entity_device_class = entity.original_device_class or entity.device_class
            if device_classes and entity_device_class not in device_classes:
                continue

            state = self._get_entity_state(entity.entity_id)
            if state and state.state == "on":
                return True

        return False

    def get_area_illuminance(self, area_id: str) -> float | None:
        """
        Get average illuminance (lux) for an area.

        This calculates the average lux value from all illuminance sensors in the area.
        Used for light learning context capture.

        Args:
            area_id: The area ID to check

        Returns:
            Average lux value, or None if no illuminance sensors found
        """
        # Get entities in this area
        area_entities_map = self._get_monitored_entities()
        entity_ids = area_entities_map.get(area_id, [])

        lux_values = []

        # Check illuminance sensors
        for entity_id in entity_ids:
            state = self._get_entity_state(entity_id)
            if not state:
                continue

            domain = split_entity_id(entity_id)[0]

            # Check sensor with illuminance device class
            if domain == "sensor":
                device_class = self._get_device_class(state)
                if device_class == "illuminance":
                    try:
                        lux = float(state.state)
                        lux_values.append(lux)
                    except (ValueError, TypeError):
                        continue

        # Return average if we have values
        if lux_values:
            return sum(lux_values) / len(lux_values)

        return None

    def get_sun_elevation(self) -> float | None:
        """
        Get the current sun elevation angle.

        This is used as a fallback for illuminance when no area sensors are available.
        The sun elevation indicates how high the sun is in the sky.

        Returns:
            Sun elevation in degrees above horizon, or None if sun.sun entity not available
        """
        sun_state = self._get_entity_state("sun.sun")
        if not sun_state:
            return None

        try:
            elevation = sun_state.attributes.get("elevation")
            if elevation is not None:
                return float(elevation)
        except (ValueError, TypeError):
            pass

        return None

    def get_area_environmental_state(self, area_id: str) -> dict[str, Any]:
        """
        Get the complete area environmental state for an area.

        Computes:
        - illuminance: Average lux value from sensors
        - temperature: Temperature from configured sensor or average
        - humidity: Humidity from configured sensor or average
        - sun_elevation: Current sun angle
        - is_dark: True if lux < 20 OR sun < 3 degrees
        - is_bright: True if lux > 100 AND sun > 10 degrees

        Args:
            area_id: The area ID to check

        Returns:
            Dictionary with all environmental data
        """
        illuminance = self.get_area_illuminance(area_id)
        temperature = self.get_area_temperature(area_id)
        humidity = self.get_area_humidity(area_id)
        sun_elevation = self.get_sun_elevation()

        is_dark = False
        is_bright = False

        if illuminance is not None and sun_elevation is not None:
            is_dark = illuminance < 20 or sun_elevation < 3
            is_bright = illuminance > 100 and sun_elevation > 10
        elif illuminance is not None:
            is_dark = illuminance < 20
            is_bright = illuminance > 100
        elif sun_elevation is not None:
            is_dark = sun_elevation < 3
            is_bright = sun_elevation > 10

        return {
            "illuminance": illuminance,
            "temperature": temperature,
            "humidity": humidity,
            "sun_elevation": sun_elevation,
            "is_dark": is_dark,
            "is_bright": is_bright,
        }

    def get_area_temperature(self, area_id: str) -> float | None:
        """
        Get temperature for an area.

        Priority:
        1. User-configured temperature sensor from area registry
        2. Average of all temperature sensors in the area

        Args:
            area_id: The area ID to check

        Returns:
            Temperature value, or None if no temperature sensors found
        """
        area = self._area_registry.async_get_area(area_id)
        if area and area.temperature_entity_id:
            state = self._get_entity_state(area.temperature_entity_id)
            if state:
                try:
                    return round(float(state.state), 1)
                except (ValueError, TypeError):
                    pass

        area_entities_map = self._get_monitored_entities()
        entity_ids = area_entities_map.get(area_id, [])

        temp_values = []

        for entity_id in entity_ids:
            state = self._get_entity_state(entity_id)
            if not state:
                continue

            domain = split_entity_id(entity_id)[0]

            if domain == "sensor":
                device_class = self._get_device_class(state)
                if device_class == "temperature":
                    try:
                        temp = float(state.state)
                        temp_values.append(temp)
                    except (ValueError, TypeError):
                        continue

        if temp_values:
            return round(sum(temp_values) / len(temp_values), 1)

        return None

    def get_area_humidity(self, area_id: str) -> float | None:
        """
        Get humidity for an area.

        Priority:
        1. User-configured humidity sensor from area registry
        2. Average of all humidity sensors in the area

        Args:
            area_id: The area ID to check

        Returns:
            Humidity value, or None if no humidity sensors found
        """
        area = self._area_registry.async_get_area(area_id)
        if area and area.humidity_entity_id:
            state = self._get_entity_state(area.humidity_entity_id)
            if state:
                try:
                    return round(float(state.state), 1)
                except (ValueError, TypeError):
                    pass

        area_entities_map = self._get_monitored_entities()
        entity_ids = area_entities_map.get(area_id, [])

        humidity_values = []

        for entity_id in entity_ids:
            state = self._get_entity_state(entity_id)
            if not state:
                continue

            domain = split_entity_id(entity_id)[0]

            if domain == "sensor":
                device_class = self._get_device_class(state)
                if device_class == "humidity":
                    try:
                        humidity = float(state.state)
                        humidity_values.append(humidity)
                    except (ValueError, TypeError):
                        continue

        if humidity_values:
            return round(sum(humidity_values) / len(humidity_values), 1)

        return None

    def get_area_entities(
        self,
        area_id: str,
        domain: str | None = None,
        device_class: str | None = None,
    ) -> list[str]:
        """
        Get all entity IDs in an area, optionally filtered by domain and device_class.

        Args:
            area_id: The area ID to search
            domain: Optional domain filter (e.g., "light", "binary_sensor")
            device_class: Optional device class filter (e.g., "motion", "illuminance")

        Returns:
            List of entity IDs matching the filters
        """
        matching_entities = []

        for entity in self._entity_registry.entities.values():
            entity_area_id = self._get_entity_area_id(entity)

            if entity_area_id != area_id:
                continue

            if domain is not None:
                entity_domain = entity.domain
                if entity_domain != domain:
                    continue

            if device_class is not None:
                entity_device_class = (
                    entity.original_device_class or entity.device_class
                )
                if entity_device_class != device_class:
                    continue

            matching_entities.append(entity.entity_id)

        return matching_entities

    def get_tracking_entities(self, area_id: str) -> list[str]:
        """
        Get entity IDs used for activity tracking and environmental state in an area.

        Returns entities from MONITORED_DOMAINS (motion, presence, occupancy,
        illuminance, media_player) that are actually tracked for area state.

        Args:
            area_id: The area ID

        Returns:
            List of entity IDs used for tracking
        """
        area_entities_map = self._get_monitored_entities()
        return area_entities_map.get(area_id, [])
