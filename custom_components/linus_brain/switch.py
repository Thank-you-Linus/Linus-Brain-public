"""
Switch platform for Linus Brain automation engine.

Provides per-area automation toggle switches (switch.linus_autolight_{area}).
Each switch controls whether automation rules are active for a specific area.
"""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from propcache import cached_property

from .const import DEFAULT_ACTIVITY_RULES, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up Linus Brain switches from a config entry.

    Creates AutoLight switches for areas with lights and presence sensors.

    Args:
        hass: Home Assistant instance
        entry: Config entry
        async_add_entities: Callback to add entities
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    area_manager = coordinator.area_manager
    areas = area_manager.get_light_automation_eligible_areas()

    switches = []
    switches_by_area = {}
    for area_id, area_name in areas.items():
        switch = LinusAutoLightSwitch(hass, entry, area_id, area_name)
        switches.append(switch)
        switches_by_area[area_id] = switch

    if switches:
        hass.data[DOMAIN][entry.entry_id]["switches"] = switches_by_area
        async_add_entities(switches)
        _LOGGER.info(
            f"Added {len(switches)} Linus AutoLight switches for eligible areas"
        )
    else:
        _LOGGER.warning(
            "No eligible areas found for light automation. Areas need both light entities and presence sensors."
        )


class LinusAutoLightSwitch(SwitchEntity):
    """
    Per-area automation toggle switch.

    Controls whether automation rules are active for a specific area.
    Stores rule metadata in attributes for offline persistence.
    """

    _attr_icon = "mdi:lightbulb-auto"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        area_id: str,
        area_name: str,
    ) -> None:
        """
        Initialize the switch.

        Args:
            hass: Home Assistant instance
            entry: Config entry
            area_id: Area ID
            area_name: Human-readable area name
        """
        self.hass = hass
        self._entry = entry
        self._area_id = area_id
        self._area_name = area_name
        self._attr_is_on = False
        self._translations: dict[str, Any] | None = None
        self._last_action: dict[str, Any] | None = None

        self._attr_unique_id = f"{DOMAIN}_feature_autolight_{area_id}"
        self._attr_name = f"Linus Brain AutoLight {area_name}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }

        self._rule_data: dict[str, Any] = {
            "area_id": area_id,
            "area_name": area_name,
            "activity_rules": DEFAULT_ACTIVITY_RULES,
            "enabled": False,
            "source": "local_default",
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        rule_engine = self.hass.data[DOMAIN][self._entry.entry_id].get("rule_engine")
        if rule_engine:
            if self._attr_is_on:
                await rule_engine.enable_area(self._area_id)
            else:
                await rule_engine.disable_area(self._area_id)
            _LOGGER.debug(
                f"Synchronized initial state for {self.entity_id}: "
                f"is_on={self._attr_is_on}, area_enabled={self._attr_is_on}"
            )

    def _get_translations(self) -> dict[str, Any]:
        """Get translations for current language."""
        if self._translations is None:
            lang = self.hass.config.language
            if lang not in ["en", "fr"]:
                lang = "en"

            translations_en = {
                "conditions": {
                    "motion_detected": "motion detected",
                    "low_light": "light level below {value} lux",
                    "sun_below": "sun elevation below {value}°",
                },
                "actions": {
                    "turn_on_lights": "Turn on lights",
                    "turn_off_lights": "Turn off lights",
                },
                "connectors": {"when": "when", "and": "and", "or": "or"},
                "activity_labels": {
                    "none": "Clear",
                    "presence": "Presence",
                    "occupation": "Occupation",
                },
            }

            translations_fr = {
                "conditions": {
                    "motion_detected": "mouvement détecté",
                    "low_light": "luminosité inférieure à {value} lux",
                    "sun_below": "élévation soleil inférieure à {value}°",
                },
                "actions": {
                    "turn_on_lights": "Allumer les lumières",
                    "turn_off_lights": "Éteindre les lumières",
                },
                "connectors": {"when": "quand", "and": "et", "or": "ou"},
                "activity_labels": {
                    "none": "Dégagé",
                    "presence": "Présence",
                    "occupation": "Occupation",
                },
            }

            self._translations = translations_fr if lang == "fr" else translations_en

        return self._translations

    def _parse_condition_summary(
        self, condition: dict[str, Any], skip_motion: bool = False
    ) -> str:
        """Parse a single condition into human-readable text with symbols."""
        cond_type = condition.get("condition", "")

        if cond_type == "and":
            subconditions = condition.get("conditions", [])
            parts = [
                self._parse_condition_summary(c, skip_motion) for c in subconditions
            ]
            parts = [p for p in parts if p]
            return " & ".join(parts)

        elif cond_type == "or":
            subconditions = condition.get("conditions", [])
            parts = [
                self._parse_condition_summary(c, skip_motion) for c in subconditions
            ]
            parts = [p for p in parts if p]
            if not parts:
                return ""
            joined = " | ".join(parts)
            return f"({joined})"

        elif cond_type == "state":
            entity_id = condition.get("entity_id", "")
            state = condition.get("state", "")
            duration = condition.get("for", {})

            if (
                condition.get("domain") == "binary_sensor"
                and condition.get("device_class") == "motion"
            ):
                if skip_motion:
                    return ""
                if duration:
                    mins = duration.get("minutes", 0)
                    secs = duration.get("seconds", 0)
                    if mins:
                        return f"motion detected >{mins}m"
                    elif secs:
                        return f"motion detected >{secs}s"
                return "motion detected" if state == "on" else "no motion"

            if duration:
                mins = duration.get("minutes", 0)
                secs = duration.get("seconds", 0)
                if mins:
                    return f"{entity_id} = {state} >{mins}m"
                elif secs:
                    return f"{entity_id} = {state} >{secs}s"

            return f"{entity_id} = {state}"

        elif cond_type == "numeric_state":
            entity_id = condition.get("entity_id", "")
            attr = condition.get("attribute", "")

            if "sun.sun" in entity_id:
                if "below" in condition:
                    return f"sun elevation <{condition['below']}°"
                elif "above" in condition:
                    return f"sun elevation >{condition['above']}°"
            elif (
                condition.get("device_class") == "illuminance"
                or "illuminance" in entity_id.lower()
            ):
                if "below" in condition:
                    return f"illuminance <{condition['below']} lux"
                elif "above" in condition:
                    return f"illuminance >{condition['above']} lux"

            if "below" in condition:
                return f"{attr or entity_id} <{condition['below']}"
            elif "above" in condition:
                return f"{attr or entity_id} >{condition['above']}"

        return ""

    def _parse_action_summary(self, action: dict[str, Any]) -> str:
        """Parse a single action into descriptive text with emojis."""
        service = action.get("service", "")
        data = action.get("data", {})

        if "light.turn_on" in service:
            parts = ["💡 Turn on lights"]
            if "brightness_pct" in data:
                parts.append(f"{data['brightness_pct']}%")
            elif "brightness" in data:
                pct = int((data["brightness"] / 255) * 100)
                parts.append(f"{pct}%")
            return " ".join(parts)
        elif "light.turn_off" in service:
            return "💡 Turn off lights"

        return service.split(".")[-1]

    def _format_rule_summary(
        self, rule_data: dict[str, Any], skip_motion: bool = False
    ) -> str:
        """Format single rule as compact summary."""
        if not rule_data:
            return ""

        conditions = rule_data.get("conditions", rule_data.get("condition", []))
        if not conditions:
            condition_text = ""
        else:
            if isinstance(conditions, list):
                main_condition = conditions[0]
            else:
                main_condition = conditions
            condition_text = self._parse_condition_summary(main_condition, skip_motion)

        actions = rule_data.get("actions", rule_data.get("action", []))
        if not actions:
            action_text = "💡 Turn on lights"
        else:
            if isinstance(actions, list):
                action_text = self._parse_action_summary(actions[0])
            else:
                action_text = self._parse_action_summary(actions)

        if condition_text:
            return f"{action_text} if {condition_text}"
        else:
            return action_text

    def _format_activity_rule_summaries(self) -> dict[str, str]:
        """Format multiple activity-based rules into compact summaries."""
        activity_rules = self._rule_data.get("activity_rules", {})

        summaries = {}
        for activity_type, rule_data in activity_rules.items():
            if rule_data:
                summary = self._format_rule_summary(rule_data, skip_motion=True)
                summaries[activity_type] = summary

        return summaries

    @cached_property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return rule data as attributes with compact activity summaries."""
        if self._rule_data:
            summaries = self._format_activity_rule_summaries()

            attrs = {
                "enabled": self._rule_data.get("enabled", True),
            }

            for activity_type, summary in summaries.items():
                attrs[activity_type] = summary

            if self._rule_data.get("source"):
                attrs["source"] = self._rule_data.get("source")

            activity_rules = self._rule_data.get("activity_rules", {})
            allowed_actions = []
            for activity_id, rule in activity_rules.items():
                actions = rule.get("actions", [])
                for action in actions:
                    action_summary = self._parse_action_summary(action)
                    if action_summary and action_summary not in allowed_actions:
                        allowed_actions.append(action_summary)

            if allowed_actions:
                attrs["allowed_actions"] = allowed_actions

            if self._last_action:
                attrs["last_action"] = self._last_action.get("activity")
                attrs["last_action_time"] = self._last_action.get("timestamp")

            return attrs
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """
        Turn the switch on.

        Activates automation rules for this area and immediately evaluates current activity.
        """
        _LOGGER.info(f"Enabling automation for area: {self._area_name}")
        self._attr_is_on = True
        self.async_write_ha_state()

        rule_engine = self.hass.data[DOMAIN][self._entry.entry_id].get("rule_engine")
        if rule_engine:
            await rule_engine.enable_area(self._area_id)
            await rule_engine._async_evaluate_and_execute(self._area_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """
        Turn the switch off.

        Deactivates automation rules for this area.
        """
        _LOGGER.info(f"Disabling automation for area: {self._area_name}")
        self._attr_is_on = False
        self.async_write_ha_state()

        rule_engine = self.hass.data[DOMAIN][self._entry.entry_id].get("rule_engine")
        if rule_engine:
            await rule_engine.disable_area(self._area_id)

    def update_rule_data(self, rule_data: dict[str, Any]) -> None:
        """
        Update rule metadata stored in attributes.

        Args:
            rule_data: Rule metadata (rule_id, version, conditions, actions, etc.)
        """
        self._rule_data = rule_data
        if hasattr(self, "_attr_extra_state_attributes"):
            delattr(self, "_attr_extra_state_attributes")
        if "extra_state_attributes" in self.__dict__:
            del self.__dict__["extra_state_attributes"]
        self.async_write_ha_state()
        _LOGGER.debug(f"Updated rule data for {self.entity_id}")

    def update_last_action(self, last_action: dict[str, Any]) -> None:
        """
        Update last action executed.

        Args:
            last_action: Last action info (activity, timestamp, actions)
        """
        self._last_action = last_action
        if hasattr(self, "_attr_extra_state_attributes"):
            delattr(self, "_attr_extra_state_attributes")
        if "extra_state_attributes" in self.__dict__:
            del self.__dict__["extra_state_attributes"]
        self.async_write_ha_state()
        _LOGGER.debug(f"Updated last action for {self.entity_id}")
