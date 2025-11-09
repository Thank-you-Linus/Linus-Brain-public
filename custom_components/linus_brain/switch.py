"""
Switch platform for Linus Brain automation engine.

Provides per-area feature flag switches (switch.linus_brain_{area}_{feature}).
Each switch controls whether a specific feature/app is active for a specific area.
Activities (movement/inactive/empty) remain always active.
"""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, get_area_device_info  # type: ignore[attr-defined]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up Linus Brain switches from a config entry.

    Creates feature flag switches for all areas and available features.

    Args:
        hass: Home Assistant instance
        entry: Config entry
        async_add_entities: Callback to add entities
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    feature_flag_manager = coordinator.feature_flag_manager

    # Get all areas and feature definitions
    area_manager = coordinator.area_manager
    all_areas = area_manager.get_all_areas()
    feature_definitions = feature_flag_manager.get_feature_definitions()

    switches = []
    switches_by_key = {}

    # Create one switch per area per feature
    for area_id in all_areas:
        for feature_id, feature_def in feature_definitions.items():
            switch = LinusBrainFeatureSwitch(
                hass, entry, area_id, feature_id, feature_def
            )
            switches.append(switch)
            switch_key = f"{area_id}_{feature_id}"
            switches_by_key[switch_key] = switch

    if switches:
        hass.data[DOMAIN][entry.entry_id]["feature_switches"] = switches_by_key
        async_add_entities(switches)
        _LOGGER.info(
            f"Added {len(switches)} Linus Brain feature switches for {len(all_areas)} areas"
        )
    else:
        _LOGGER.warning("No feature switches created - no areas or features available")


class LinusBrainFeatureSwitch(RestoreEntity, SwitchEntity):
    """
    Per-area feature flag switch.

    Controls whether a specific feature/app is active for a specific area.
    Activities (movement/inactive/empty) remain always active regardless of switch state.
    Restores state after Home Assistant restart.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        area_id: str,
        feature_id: str,
        feature_def: dict[str, Any],
    ) -> None:
        """
        Initialize feature switch.

        Args:
            hass: Home Assistant instance
            entry: Config entry
            area_id: Area ID
            feature_id: Feature ID
            feature_def: Feature definition dictionary
        """
        self.hass = hass
        self._entry = entry
        self._area_id = area_id
        self._feature_id = feature_id
        self._feature_def = feature_def

        # Default OFF, will be restored from feature flag manager
        self._attr_is_on = False
        self._translations: dict[str, Any] | None = None

        # Get proper area name from area registry
        area_registry = ar.async_get(hass)
        area = area_registry.async_get_area(area_id)
        area_name = area.name if area else area_id.replace("_", " ").title()

        # Set entity attributes
        self._attr_unique_id = f"{DOMAIN}_feature_{feature_id}_{area_id}"
        self._attr_has_entity_name = True
        self._attr_suggested_object_id = f"{DOMAIN}_feature_{feature_id}_{area_id}"  # Force English entity_id

        # Use translation key for proper localization
        self._attr_translation_key = f"feature_{feature_id}"
        self._attr_translation_placeholders = {"area_name": area_name}

        # Associate with area-specific device
        self._attr_device_info = get_area_device_info(  # type: ignore[assignment]
            entry.entry_id, area_id, area_name
        )

        # Mark as config entity
        # self._attr_entity_category = EntityCategory.CONFIG

        # Set icon based on feature type
        if feature_id == "automatic_lighting":
            self._attr_icon = "mdi:lightbulb-auto"
        else:
            self._attr_icon = "mdi:application-cog"

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass - restore state using RestoreEntity."""
        await super().async_added_to_hass()

        # Try to restore previous state from Home Assistant
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"
            _LOGGER.info(
                f"Restored feature state for {self.entity_id}: {self._attr_is_on} "
                f"(from previous state: {last_state.state})"
            )
        else:
            # No previous state - use feature definition default
            self._attr_is_on = self._feature_def.get("default_enabled", False)
            _LOGGER.info(
                f"No previous state for {self.entity_id}, using default: {self._attr_is_on}"
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the feature on and evaluate rules immediately."""
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.info(f"Feature {self._feature_id} ENABLED for area {self._area_id}")
        
        # Trigger immediate rule evaluation if rule engine is available
        from .const import DOMAIN
        rule_engine = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("rule_engine")
        if rule_engine:
            _LOGGER.info(
                f"Triggering immediate rule evaluation for {self._area_id} after enabling {self._feature_id}"
            )
            # Schedule evaluation in background to avoid blocking switch response
            self.hass.async_create_task(
                rule_engine._async_evaluate_and_execute(self._area_id, is_environmental=False)
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """
        Turn the feature off.
        
        Note: This only disables future automation. It does not change the current
        state of devices (e.g., lights remain on if they were on). This allows users
        to disable automation without disrupting their current environment.
        """
        self._attr_is_on = False
        self.async_write_ha_state()
        _LOGGER.info(
            f"Feature {self._feature_id} DISABLED for area {self._area_id}. "
            "Current device states are preserved."
        )
