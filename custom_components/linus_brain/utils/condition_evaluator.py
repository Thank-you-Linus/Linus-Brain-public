"""
Condition evaluator for automation rules.

Evaluates rule conditions against current Home Assistant state.
Supports multi-condition rules with AND/OR logic and dynamic entity resolution.
"""

import logging
import re
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import template
from homeassistant.util import dt as dt_util

from .entity_resolver import EntityResolver

_LOGGER = logging.getLogger(__name__)


class ConditionEvaluator:
    """
    Evaluates automation rule conditions.

    Supports various condition types:
    - state: Entity state equals value
    - numeric_state: Entity state compared numerically
    - template: Jinja2 template evaluation
    - time: Time-based conditions
    - zone: Zone-based conditions (future)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_resolver: EntityResolver,
        activity_tracker=None,
    ) -> None:
        """
        Initialize the condition evaluator.

        Args:
            hass: Home Assistant instance
            entity_resolver: Entity resolver for generic selectors
            activity_tracker: Optional ActivityTracker instance for activity conditions
        """
        self.hass = hass
        self.entity_resolver = entity_resolver
        self.activity_tracker = activity_tracker

    async def evaluate_conditions(
        self,
        conditions: list[dict[str, Any]],
        area_id: str,
        logic: str = "and",
    ) -> bool:
        """
        Evaluate a list of conditions with AND/OR logic.

        Args:
            conditions: List of condition dictionaries
            area_id: Area context for entity resolution
            logic: "and" or "or" logic for combining conditions

        Returns:
            True if conditions are met, False otherwise
        """
        if not conditions:
            return True

        _LOGGER.debug(f"Resolving conditions for area {area_id}: {conditions}")

        resolved_conditions = self.entity_resolver.resolve_nested_conditions(
            conditions, area_id
        )

        _LOGGER.debug(f"Resolved conditions for area {area_id}: {resolved_conditions}")

        results = []
        for condition in resolved_conditions:
            try:
                result = await self._evaluate_single_condition(condition)
                results.append(result)

                _LOGGER.debug(f"Condition {condition} evaluated to {result}")

                if logic == "or" and result:
                    return True

                if logic == "and" and not result:
                    return False

            except Exception as err:
                _LOGGER.error(f"Failed to evaluate condition {condition}: {err}")
                results.append(False)

                if logic == "and":
                    return False

        if logic == "and":
            return all(results)
        else:
            return any(results)

    async def _evaluate_single_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate a single condition.

        Args:
            condition: Condition dictionary with type and parameters

        Returns:
            True if condition is met, False otherwise
        """
        condition_type = condition.get("condition")

        if condition_type == "state":
            return await self._evaluate_state_condition(condition)

        elif condition_type == "numeric_state":
            return await self._evaluate_numeric_state_condition(condition)

        elif condition_type == "template":
            return await self._evaluate_template_condition(condition)

        elif condition_type == "time":
            return await self._evaluate_time_condition(condition)

        elif condition_type == "activity":
            return await self._evaluate_activity_condition(condition)

        elif condition_type == "area_state":
            return await self._evaluate_area_state_condition(condition)

        else:
            _LOGGER.warning(f"Unknown condition type: {condition_type}")
            return False

    async def _evaluate_state_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate state condition (entity state equals value).

        Args:
            condition: Condition with entity_id and state

        Returns:
            True if entity state matches
        """
        entity_id = condition.get("entity_id")
        expected_state = condition.get("state")

        if not entity_id or expected_state is None:
            _LOGGER.debug(f"State condition missing entity_id or state: {condition}")
            return False

        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.debug(f"Entity {entity_id} not found")
            return False

        for_duration = condition.get("for")
        if for_duration:
            _LOGGER.warning("Duration conditions ('for') not yet supported")

        result = state.state == str(expected_state)
        _LOGGER.debug(
            f"State check: {entity_id} = {state.state}, expected = {expected_state}, match = {result}"
        )

        return result

    async def _evaluate_numeric_state_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate numeric state condition (above/below thresholds).

        Args:
            condition: Condition with entity_id, above/below

        Returns:
            True if numeric condition is met
        """
        entity_id = condition.get("entity_id")
        above = condition.get("above")
        below = condition.get("below")

        if not entity_id:
            return False

        state = self.hass.states.get(entity_id)
        if state is None:
            return False

        try:
            value = float(state.state)
        except (ValueError, TypeError):
            _LOGGER.debug(f"Cannot convert {entity_id} state to number: {state.state}")
            return False

        if above is not None and value <= float(above):
            return False

        if below is not None and value >= float(below):
            return False

        return True

    async def _evaluate_template_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate template condition (Jinja2 template).

        Args:
            condition: Condition with value_template

        Returns:
            True if template evaluates to True
        """
        value_template = condition.get("value_template")

        if not value_template:
            return False

        try:
            tpl = template.Template(value_template, self.hass)
            result = tpl.async_render()

            return result in [True, "True", "true", "yes", "on", "1"]

        except Exception as err:
            _LOGGER.error(f"Template evaluation failed: {err}")
            return False

    async def _evaluate_time_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate time condition (before/after times).

        Args:
            condition: Condition with before/after times

        Returns:
            True if current time is within range
        """
        after = condition.get("after")
        before = condition.get("before")

        now = dt_util.now().time()

        if after:
            after_time = self._parse_time(after)
            if after_time and now < after_time:
                return False

        if before:
            before_time = self._parse_time(before)
            if before_time and now > before_time:
                return False

        return True

    def _parse_time(self, time_str: str) -> Any:
        """
        Parse time string (HH:MM:SS or HH:MM).

        Args:
            time_str: Time string

        Returns:
            time object or None
        """
        try:
            parts = time_str.split(":")
            if len(parts) == 2:
                return datetime.strptime(time_str, "%H:%M").time()
            elif len(parts) == 3:
                return datetime.strptime(time_str, "%H:%M:%S").time()
        except Exception as err:
            _LOGGER.error(f"Failed to parse time {time_str}: {err}")

        return None

    def get_referenced_entities(
        self,
        conditions: list[dict[str, Any]],
        area_id: str | None = None,
    ) -> set[str]:
        """
        Extract all entity IDs referenced in conditions.

        Used for dynamic listener registration.

        Args:
            conditions: List of condition dictionaries
            area_id: Area context for resolving generic selectors

        Returns:
            Set of entity IDs
        """
        entities = set()

        for condition in conditions:
            condition_type = condition.get("condition")

            if condition_type in ["and", "or"]:
                nested_conditions = condition.get("conditions", [])
                nested_entities = self.get_referenced_entities(
                    nested_conditions, area_id
                )
                entities.update(nested_entities)
                continue

            if condition_type in ["activity", "area_state"]:
                continue

            entity_id = condition.get("entity_id")
            if entity_id:
                entities.add(entity_id)
                continue

            domain = condition.get("domain")
            if domain and area_id:
                device_class = condition.get("device_class")
                area = condition.get("area")
                target_area_id = area_id if area == "current" or area is None else area

                resolved_entities = self.entity_resolver.resolve_entity(
                    domain=domain,
                    area_id=target_area_id,
                    device_class=device_class,
                    strategy="all",
                )

                if resolved_entities:
                    if isinstance(resolved_entities, list):
                        entities.update(resolved_entities)
                    else:
                        entities.add(resolved_entities)
                continue

            value_template = condition.get("value_template")
            if value_template:
                template_entities = self._extract_entities_from_template(value_template)
                entities.update(template_entities)

        return entities

    def _extract_entities_from_template(self, template_str: str) -> set[str]:
        """
        Extract entity IDs from Jinja2 template.

        Args:
            template_str: Template string

        Returns:
            Set of entity IDs
        """
        entities = set()

        pattern = r"states\(['\"]([a-z_]+\.[a-z0-9_]+)['\"]\)"
        matches = re.findall(pattern, template_str)
        entities.update(matches)

        pattern = r"states\.([a-z_]+)\.([a-z0-9_]+)"
        matches = re.findall(pattern, template_str)
        for domain, object_id in matches:
            entities.add(f"{domain}.{object_id}")

        return entities

    async def _evaluate_activity_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate activity condition (presence level in area).

        Args:
            condition: Condition with area_id and expected activity state
                      Format after resolution: {"condition": "activity", "area_id": "...", "activity": "presence|occupation|none"}

        Returns:
            True if activity matches expected state
        """
        expected_activity = condition.get("activity")
        area_id = condition.get("area_id")

        if not expected_activity or not area_id:
            _LOGGER.warning(
                f"Activity condition missing 'activity' or 'area_id': {condition}"
            )
            return False

        try:
            if self.activity_tracker:
                activity_level = await self.activity_tracker.get_activity_level(area_id)
            else:
                from .activity_tracker import ActivityTracker

                activity_tracker = ActivityTracker(self.hass)
                activity_level = await activity_tracker.get_activity_level(area_id)

            return activity_level == expected_activity

        except Exception as err:
            _LOGGER.error(f"Failed to evaluate activity condition: {err}")
            return False

    async def _evaluate_area_state_condition(
        self,
        condition: dict[str, Any],
    ) -> bool:
        """
        Evaluate area state condition (environmental attributes).

        Args:
            condition: Condition with area_id and state/attribute to check
                      Format after resolution: {"condition": "area_state", "area_id": "...", "state": "is_dark|is_bright"}
                      OR: {"condition": "area_state", "area_id": "...", "attribute": "is_dark|is_bright"}

        Returns:
            True if area state matches expected value
        """
        state_attr = condition.get("state") or condition.get("attribute")
        area_id = condition.get("area_id")

        if not state_attr or not area_id:
            _LOGGER.warning(
                f"Area state condition missing 'state'/'attribute' or 'area_id': {condition}"
            )
            return False

        from .area_manager import AreaManager

        try:
            area_manager = AreaManager(self.hass)
            area_state = area_manager.get_area_environmental_state(area_id)

            if state_attr not in area_state:
                _LOGGER.warning(f"Unknown area state attribute: {state_attr}")
                return False

            value = area_state[state_attr]

            if isinstance(value, bool):
                return value

            _LOGGER.warning(
                f"Area state attribute {state_attr} is not boolean: {value}"
            )
            return False

        except Exception as err:
            _LOGGER.error(f"Failed to evaluate area state condition: {err}")
            return False
