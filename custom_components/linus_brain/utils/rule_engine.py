"""
Rule Engine for Linus Brain automation system.

Orchestrates local automation with app-based architecture:
- Dynamic activity evaluation
- App assignment management
- Condition evaluation
- Action execution
- Cooldown protection
- Offline-first architecture
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import area_registry
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .action_executor import ActionExecutor
from .app_storage import AppStorage
from .condition_evaluator import ConditionEvaluator
from .entity_resolver import EntityResolver

_LOGGER = logging.getLogger(__name__)

COOLDOWN_SECONDS = 30
COOLDOWN_ENVIRONMENTAL_SECONDS = 300  # 5 minutes for environmental transitions
DEBOUNCE_SECONDS = 2


class RuleEngine:
    """
    App-based automation engine.

    Manages per-area app assignments with:
    - Dynamic activity evaluation
    - Dynamic listeners on referenced entities
    - Condition evaluation with AND/OR logic
    - Action execution via service calls
    - Cooldown and debounce protection
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        activity_tracker=None,
        app_storage=None,
        area_manager=None,
        feature_flag_manager=None,
    ) -> None:
        """
        Initialize the rule engine.

        Args:
            hass: Home Assistant instance
            entry_id: Config entry ID
            activity_tracker: ActivityTracker instance for activity-based automation
            app_storage: AppStorage instance (shared from coordinator)
            area_manager: AreaManager instance for entity lookup
        """
        self.hass = hass
        self.entry_id = entry_id
        self.activity_tracker = activity_tracker
        self.area_manager = area_manager
        self.feature_flag_manager = feature_flag_manager

        self.app_storage = app_storage if app_storage else AppStorage(hass)
        self.entity_resolver = EntityResolver(hass)
        self.condition_evaluator = ConditionEvaluator(
            hass, self.entity_resolver, activity_tracker
        )
        self.action_executor = ActionExecutor(hass, self.entity_resolver)

        self._assignments: dict[str, dict[str, Any]] = {}
        self._listeners: dict[str, list[Any]] = {}
        self._last_triggered: dict[str, datetime] = {}
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._last_actions: dict[str, dict[str, Any]] = {}
        
        # Environmental state tracking for transition detection
        # Format: {area_id: {"is_dark": bool}}
        self._previous_env_state: dict[str, dict[str, bool]] = {}

        self._stats = {
            "total_triggers": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "cooldown_blocks": 0,
        }

    async def async_initialize(self) -> None:
        """
        Initialize the rule engine.

        Loads apps/assignments from AppStorage and registers listeners.
        If no assignments exist, creates default automatic_lighting assignments for all areas.
        """
        _LOGGER.info("Initializing rule engine")

        if self.activity_tracker:
            await self.activity_tracker.async_initialize()
            _LOGGER.info("ActivityTracker initialized")

        self._assignments = self.app_storage.get_assignments()

        if not self._assignments:
            _LOGGER.info(
                "No assignments found in storage, creating default assignments"
            )
            await self._ensure_default_assignments()

        for area_id in self._assignments.keys():
            assignment = self._assignments[area_id]
            if assignment.get("enabled", True):
                await self.enable_area(area_id)

        _LOGGER.info(
            f"Rule engine initialized: {len(self._assignments)} assignments, "
            "initialized"
        )

    async def async_shutdown(self) -> None:
        """
        Shutdown the rule engine.

        Removes all listeners and cancels pending tasks.
        """
        _LOGGER.info("Shutting down rule engine")

        for area_id in self._assignments.keys():
            await self.disable_area(area_id)

        for task in self._debounce_tasks.values():
            if not task.done():
                task.cancel()

        self._debounce_tasks.clear()

    async def _ensure_default_assignments(self) -> None:
        """
        Ensure all areas have app assignments.

        Creates default automatic_lighting assignments for areas without assignments.
        Also ensures the automatic_lighting app exists in storage (loads fallback if needed).
        Uses cloud-first strategy: tries Supabase first, then local storage.
        """
        try:
            area_reg = area_registry.async_get(self.hass)
            areas = area_reg.async_list_areas()

            if not areas:
                _LOGGER.info("No areas found, skipping default assignment creation")
                return

            # Ensure automatic_lighting app AND required activities exist
            # This handles the case where cloud returns empty but user has areas with entities
            app = self.app_storage.get_app("automatic_lighting")
            if not app:
                _LOGGER.info(
                    "automatic_lighting app not found in storage, loading fallback app and activities"
                )
                from ..const import DEFAULT_AUTOLIGHT_APP, DEFAULT_ACTIVITY_TYPES
                
                # Load default app
                self.app_storage.set_app("automatic_lighting", DEFAULT_AUTOLIGHT_APP)
                
                # Ensure required activities exist (movement, inactive, empty)
                for activity_id in ["movement", "inactive", "empty"]:
                    if not self.app_storage.get_activity(activity_id):
                        if activity_id in DEFAULT_ACTIVITY_TYPES:
                            self.app_storage.set_activity(
                                activity_id, DEFAULT_ACTIVITY_TYPES[activity_id]
                            )
                
                await self.app_storage.async_save()
                _LOGGER.info(
                    "Loaded fallback: 1 app (automatic_lighting) + 3 activities (movement, inactive, empty)"
                )

            from ..const import DOMAIN

            entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
            coordinator = entry_data.get("coordinator")

            created_count = 0
            for area in areas:
                if area.id in self._assignments:
                    continue

                assignment_data = {
                    "area_id": area.id,
                    "app_id": "automatic_lighting",
                    "enabled": True,
                    "created_at": dt_util.utcnow().isoformat(),
                    "is_default": True,
                }

                if coordinator:
                    try:
                        instance_id = await coordinator.get_or_create_instance_id()
                        await coordinator.supabase_client.save_area_assignment(
                            instance_id=instance_id,
                            area_id=area.id,
                            app_id="automatic_lighting",
                            enabled=True,
                        )
                        _LOGGER.debug(
                            f"Created cloud assignment for area {area.id}: automatic_lighting"
                        )
                    except Exception as err:
                        _LOGGER.warning(
                            f"Failed to create cloud assignment for {area.id}: {err}"
                        )

                self.app_storage.set_assignment(area.id, assignment_data)
                self._assignments[area.id] = assignment_data
                created_count += 1

            await self.app_storage.async_save()
            _LOGGER.info(
                f"Created {created_count} default automatic_lighting assignments"
            )

        except Exception as err:
            _LOGGER.error(f"Failed to create default assignments: {err}")

    def _get_area_presence_entities(self, area_id: str) -> set[str]:
        """
        Get all presence detection entities for an area.

        Uses dynamic get_presence_detection_domains() to find:
        - binary_sensor.motion
        - binary_sensor.presence
        - binary_sensor.occupancy
        - media_player (any)
        - Plus any domains from activity detection_conditions

        Args:
            area_id: Area ID

        Returns:
            Set of entity IDs
        """
        from .area_manager import get_presence_detection_domains

        entities: set[str] = set()

        if not self.area_manager:
            _LOGGER.warning("No area_manager available for presence entity lookup")
            return entities

        presence_domains = get_presence_detection_domains()
        for domain, device_classes in presence_domains.items():
            if not device_classes:
                domain_entities = self.area_manager.get_area_entities(
                    area_id, domain=domain
                )
                entities.update(domain_entities)
            else:
                for device_class in device_classes:
                    class_entities = self.area_manager.get_area_entities(
                        area_id, domain=domain, device_class=device_class
                    )
                    entities.update(class_entities)

        return entities

    def _get_area_environmental_entities(self, area_id: str) -> set[str]:
        """
        Get all environmental entities for an area that affect area_state conditions.

        Returns entities used in get_area_environmental_state():
        - sensor.illuminance (all illuminance sensors in area)
        - sun.sun (global sun entity)

        Args:
            area_id: Area ID

        Returns:
            Set of entity IDs
        """
        entities: set[str] = set()

        if not self.area_manager:
            _LOGGER.warning("No area_manager available for environmental entity lookup")
            return entities

        # Get illuminance sensors in the area
        illuminance_sensors = self.area_manager.get_area_entities(
            area_id, domain="sensor", device_class="illuminance"
        )
        entities.update(illuminance_sensors)

        # Add sun.sun entity (used for sun elevation)
        sun_state = self.hass.states.get("sun.sun")
        if sun_state:
            entities.add("sun.sun")

        return entities

    def _has_area_state_condition(self, conditions: list[dict[str, Any]]) -> bool:
        """
        Check if conditions list contains any area_state condition.

        Recursively searches through nested and/or conditions.

        Args:
            conditions: List of condition dictionaries

        Returns:
            True if any area_state condition found
        """
        for condition in conditions:
            condition_type = condition.get("condition")

            if condition_type == "area_state":
                return True

            if condition_type in ["and", "or"]:
                nested_conditions = condition.get("conditions", [])
                if self._has_area_state_condition(nested_conditions):
                    return True

        return False

    def _get_current_environmental_state(self, area_id: str) -> dict[str, bool]:
        """
        Get current environmental state for an area.

        Uses area_manager.get_area_environmental_state() to compute:
        - is_dark: True if illuminance < 20 lux or sun elevation < 3°

        Args:
            area_id: Area ID

        Returns:
            Dictionary with is_dark boolean value
        """
        if not self.area_manager:
            return {"is_dark": False}

        env_state = self.area_manager.get_area_environmental_state(area_id)
        return {
            "is_dark": env_state.get("is_dark", False),
        }

    def _detect_environmental_transition(
        self, area_id: str, current_state: dict[str, bool]
    ) -> str | None:
        """
        Detect environmental state transitions.

        Compares current state to previous state and returns transition type:
        - "became_dark": is_dark changed from False to True

        Args:
            area_id: Area ID
            current_state: Current environmental state {"is_dark": bool}

        Returns:
            Transition type string or None if no transition detected
        """
        previous_state = self._previous_env_state.get(area_id)
        
        if previous_state is None:
            # First time seeing this area, no transition
            return None

        # Check for darkness transition
        if not previous_state.get("is_dark") and current_state.get("is_dark"):
            return "became_dark"

        return None

    async def enable_area(self, area_id: str) -> None:
        """
        Enable automation for an area.

        Registers listeners on:
        1. Presence detection entities (motion, occupancy, media_player)
        2. Entities referenced in activity_actions conditions
        3. Environmental entities (illuminance, sun) when area_state conditions are used

        Args:
            area_id: Area ID
        """
        if area_id not in self._assignments:
            _LOGGER.warning(f"No assignment found for area: {area_id}")
            return

        # Areas are always "enabled" for activities, feature flags control app execution
        _LOGGER.debug(f"Enabling area {area_id} for activity tracking")

        assignment = self._assignments[area_id]
        app_id = assignment.get("app_id")

        if not app_id:
            _LOGGER.warning(f"No app_id in assignment for area {area_id}")
            return

        app = self.app_storage.get_app(app_id)
        if not app:
            _LOGGER.warning(f"App {app_id} not found for area {area_id}")
            return

        all_entities = set()

        presence_entities = self._get_area_presence_entities(area_id)
        all_entities.update(presence_entities)

        # Check if app uses area_state conditions
        uses_area_state = False
        activity_actions = app.get("activity_actions", {})
        if activity_actions:
            for activity_id, action_config in activity_actions.items():
                conditions = action_config.get("conditions", [])

                # Check if any condition uses area_state
                if self._has_area_state_condition(conditions):
                    uses_area_state = True

                condition_entities = self.condition_evaluator.get_referenced_entities(
                    conditions, area_id
                )
                all_entities.update(condition_entities)

        # If app uses area_state conditions, track environmental entities
        environmental_entities = set()
        if uses_area_state:
            environmental_entities = self._get_area_environmental_entities(area_id)
            all_entities.update(environmental_entities)

        if not all_entities:
            _LOGGER.warning(
                f"No entities to track for area {area_id} - "
                f"no presence entities and no condition entities found"
            )
            return

        listeners = []
        for entity_id in all_entities:
            listener = async_track_state_change_event(
                self.hass,
                [entity_id],
                self._async_state_change_handler,
            )
            listeners.append(listener)

        self._listeners[area_id] = listeners

        # Initialize environmental state cache for areas using area_state conditions
        if uses_area_state:
            self._previous_env_state[area_id] = self._get_current_environmental_state(
                area_id
            )
            _LOGGER.debug(
                f"Initialized environmental state cache for {area_id}: "
                f"{self._previous_env_state[area_id]}"
            )

        condition_count = len(all_entities - presence_entities - environmental_entities)
        env_count = len(environmental_entities)

        _LOGGER.info(
            f"Enabled automation for {area_id} (app: {app_id}): "
            f"tracking {len(presence_entities)} presence + "
            f"{condition_count} condition + "
            f"{env_count} environmental = {len(all_entities)} total entities"
        )

    async def disable_area(self, area_id: str) -> None:
        """
        Disable automation for an area.

        Removes all listeners for the area and clears environmental state cache.

        Args:
            area_id: Area ID
        """
        for listener in self._listeners.get(area_id, []):
            listener()

        self._listeners.pop(area_id, None)
        self._previous_env_state.pop(area_id, None)

        _LOGGER.info(f"Disabled automation for area: {area_id}")

    @callback
    def _async_state_change_handler(self, event: Event[EventStateChangedData]) -> None:
        """
        Handle entity state change events.

        Debounces and schedules condition evaluation for:
        1. Presence entities (trigger activity re-evaluation)
        2. Condition entities (trigger condition re-evaluation)
        3. Environmental entities (trigger area_state re-evaluation)

        Args:
            event: State change event
        """
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return

        affected_areas = []
        for area_id in self._assignments.keys():
            assignment = self._assignments.get(area_id, {})
            app_id = assignment.get("app_id")

            if not app_id:
                continue

            app = self.app_storage.get_app(app_id)
            if not app:
                continue

            # Check if entity is tracked for this area
            is_tracked = False

            # Check presence entities
            presence_entities = self._get_area_presence_entities(area_id)
            if entity_id in presence_entities:
                is_tracked = True

            # Check condition entities
            if not is_tracked:
                activity_actions = app.get("activity_actions", {})
                if activity_actions:
                    for activity_id, action_config in activity_actions.items():
                        conditions = action_config.get("conditions", [])
                        condition_entities = (
                            self.condition_evaluator.get_referenced_entities(
                                conditions, area_id
                            )
                        )
                        if entity_id in condition_entities:
                            is_tracked = True
                            break

            # Check environmental entities (for area_state conditions)
            if not is_tracked:
                environmental_entities = self._get_area_environmental_entities(area_id)
                if entity_id in environmental_entities:
                    is_tracked = True

            if is_tracked:
                affected_areas.append(area_id)

        for area_id in affected_areas:
            # Check if this is an environmental entity triggering a state transition
            environmental_entities = self._get_area_environmental_entities(area_id)
            is_environmental = entity_id in environmental_entities

            if is_environmental:
                # Check for environmental state transition
                current_env_state = self._get_current_environmental_state(area_id)
                transition = self._detect_environmental_transition(
                    area_id, current_env_state
                )

                # Update cache for next comparison
                self._previous_env_state[area_id] = current_env_state

                if transition:
                    _LOGGER.info(
                        f"Environmental transition detected for {area_id}: {transition} "
                        f"(triggered by {entity_id})"
                    )
                    # Environmental transition detected, trigger immediate evaluation
                    # Use separate debounce key for environmental triggers
                    key = f"{area_id}_env_transition"
                else:
                    # Environmental entity changed but no transition, skip
                    _LOGGER.debug(
                        f"Environmental entity {entity_id} changed for {area_id} "
                        f"but no transition detected, skipping"
                    )
                    continue
            else:
                # Non-environmental entity (presence or condition entity)
                key = f"{area_id}_{entity_id}"

            if key in self._debounce_tasks and not self._debounce_tasks[key].done():
                self._debounce_tasks[key].cancel()

            self._debounce_tasks[key] = asyncio.create_task(
                self._async_evaluate_rule_debounced(
                    area_id, entity_id, is_environmental=is_environmental
                )
            )

    async def _async_evaluate_rule_debounced(
        self,
        area_id: str,
        entity_id: str,
        is_environmental: bool = False,
    ) -> None:
        """
        Evaluate rule after debounce delay.

        Args:
            area_id: Area ID
            entity_id: Entity that changed
            is_environmental: True if triggered by environmental transition
        """
        await asyncio.sleep(DEBOUNCE_SECONDS)

        trigger_type = "environmental transition" if is_environmental else entity_id
        _LOGGER.info(f"Evaluating rule for {area_id} (triggered by {trigger_type})")

        await self._async_evaluate_and_execute(area_id, is_environmental=is_environmental)

    async def _async_evaluate_and_execute(
        self, area_id: str, is_environmental: bool = False
    ) -> None:
        """
        Evaluate conditions and execute actions based on current activity.

        Uses dynamic app-based architecture:
        1. Get current activity from ActivityTracker
        2. Get app assignment for area from AppStorage
        3. Get activity_actions for current activity from app
        4. Evaluate conditions (if any)
        5. Execute actions if conditions match

        Args:
            area_id: Area ID
            is_environmental: True if triggered by environmental transition
        """
        self._stats["total_triggers"] += 1

        if area_id not in self._assignments:
            _LOGGER.debug(f"No assignment for area {area_id}")
            return

        if not self.activity_tracker:
            _LOGGER.warning(
                f"Assignment exists for {area_id} but no activity tracker available"
            )
            return

        current_activity = await self.activity_tracker.async_evaluate_activity(area_id)

        assignment = self._assignments[area_id]
        app_id = assignment.get("app_id")

        if not app_id:
            _LOGGER.warning(f"No app_id in assignment for area {area_id}")
            return

        app = self.app_storage.get_app(app_id)
        if not app:
            _LOGGER.warning(f"App {app_id} not found for area {area_id}")
            return

        # Check if this app/feature is enabled for this area
        # Check the switch entity state via Home Assistant
        switch_entity_id = f"switch.linus_brain_feature_{app_id}_{area_id}"
        switch_state = self.hass.states.get(switch_entity_id)
        
        if switch_state is None:
            _LOGGER.debug(
                f"Switch {switch_entity_id} not found, assuming app {app_id} is disabled for area {area_id}"
            )
            return
        
        if switch_state.state != "on":
            _LOGGER.debug(
                f"App {app_id} not enabled for area {area_id} (switch is {switch_state.state}), skipping execution"
            )
            return

        activity_actions = app.get("activity_actions", {})
        if current_activity not in activity_actions:
            _LOGGER.debug(
                f"No actions defined for activity '{current_activity}' in app {app_id}"
            )
            return

        if not self._check_cooldown(area_id, current_activity, is_environmental):
            self._stats["cooldown_blocks"] += 1
            trigger_type = "environmental" if is_environmental else "activity"
            _LOGGER.debug(
                f"Rule {area_id}:{current_activity} ({trigger_type}) in cooldown, skipping"
            )
            return

        action_config = activity_actions[current_activity]
        conditions = action_config.get("conditions", [])
        actions = action_config.get("actions", [])
        logic = action_config.get("logic", "and")

        from ..const import DOMAIN

        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
        coordinator = entry_data.get("coordinator")
        previous_activity = (
            coordinator.previous_activities.get(area_id) if coordinator else None
        )

        try:
            conditions_met = await self.condition_evaluator.evaluate_conditions(
                conditions, area_id, logic
            )

            if conditions_met:
                _LOGGER.info(
                    f"Conditions met for {area_id} (activity: {current_activity}), executing actions"
                )

                success = await self.action_executor.execute_actions(
                    actions,
                    area_id,
                    current_activity=current_activity,
                    previous_activity=previous_activity,
                )

                if success:
                    self._stats["successful_executions"] += 1
                    self._update_last_triggered(
                        area_id, current_activity, is_environmental=is_environmental
                    )

                    self._last_actions[area_id] = {
                        "activity": current_activity,
                        "timestamp": dt_util.utcnow().isoformat(),
                        "actions": actions,
                    }
                    self._update_switch_last_action(area_id)

                    if coordinator:
                        rule_info = {
                            "rule_name": f"{app_id}:{current_activity}",
                            "timestamp": dt_util.utcnow().isoformat(),
                            "conditions_met": True,
                            "actions_count": len(actions),
                            "activity": current_activity,
                            "app_id": app_id,
                        }
                        coordinator.last_rules[area_id] = rule_info
                        _LOGGER.debug(f"Updated last_rule tracking for {area_id}")
                else:
                    self._stats["failed_executions"] += 1

            else:
                _LOGGER.debug(
                    f"Conditions not met for {area_id} (activity: {current_activity})"
                )

        except Exception as err:
            _LOGGER.error(
                f"Error evaluating app {app_id} for {area_id}:{current_activity}: {err}"
            )
            self._stats["failed_executions"] += 1

    def _check_cooldown(
        self,
        area_id: str,
        activity_type: str | None = None,
        is_environmental: bool = False,
    ) -> bool:
        """
        Check if rule is in cooldown period.

        Environmental triggers use a longer cooldown (5 minutes) than activity triggers (30 seconds).

        Args:
            area_id: Area ID
            activity_type: Optional activity type for activity-based rules
            is_environmental: True if checking environmental trigger cooldown

        Returns:
            True if not in cooldown, False if in cooldown
        """
        # Use separate cooldown key for environmental triggers
        if is_environmental:
            cooldown_key = f"{area_id}_env"
        else:
            cooldown_key = f"{area_id}_{activity_type}" if activity_type else area_id

        if cooldown_key not in self._last_triggered:
            return True

        last_trigger = self._last_triggered[cooldown_key]
        
        # Use different cooldown duration for environmental triggers
        cooldown_duration = (
            COOLDOWN_ENVIRONMENTAL_SECONDS if is_environmental else COOLDOWN_SECONDS
        )
        cooldown_until = last_trigger + timedelta(seconds=cooldown_duration)

        return dt_util.utcnow() > cooldown_until

    def _update_last_triggered(
        self,
        area_id: str,
        activity_type: str | None = None,
        is_environmental: bool = False,
    ) -> None:
        """
        Update last triggered timestamp for an area/activity.

        Args:
            area_id: Area ID
            activity_type: Optional activity type for activity-based rules
            is_environmental: True if updating environmental trigger timestamp
        """
        # Use separate cooldown key for environmental triggers
        if is_environmental:
            cooldown_key = f"{area_id}_env"
        else:
            cooldown_key = f"{area_id}_{activity_type}" if activity_type else area_id
        self._last_triggered[cooldown_key] = dt_util.utcnow()

    async def reload_assignments(self) -> int:
        """
        Reload all assignments from storage.

        Returns:
            Number of assignments reloaded
        """
        _LOGGER.info("Reloading assignments from storage")

        for area_id in list(self._assignments.keys()):
            await self.disable_area(area_id)

        self._assignments = self.app_storage.get_assignments()

        for area_id, assignment in self._assignments.items():
            if assignment.get("enabled", True):
                await self.enable_area(area_id)

        _LOGGER.info(f"Reloaded {len(self._assignments)} assignments")
        return len(self._assignments)

    def get_stats(self) -> dict[str, Any]:
        """
        Get rule engine statistics.

        Returns:
            Dictionary with statistics
        """
        return {
            "total_assignments": len(self._assignments),
            **self._stats,
        }

    async def get_assignment(self, area_id: str) -> dict[str, Any] | None:
        """
        Get assignment for an area.

        Args:
            area_id: Area ID

        Returns:
            Assignment data or None
        """
        return self._assignments.get(area_id)

    async def delete_assignment(self, area_id: str) -> bool:
        """
        Delete assignment for an area.

        Args:
            area_id: Area ID

        Returns:
            True if deleted successfully
        """
        await self.disable_area(area_id)

        if area_id in self._assignments:
            del self._assignments[area_id]

        self.app_storage.remove_assignment(area_id)
        return await self.app_storage.async_save()

    def _update_switch_attributes(
        self, area_id: str, assignment_data: dict[str, Any]
    ) -> None:
        """
        Update switch entity attributes with assignment data.

        Args:
            area_id: Area ID
            assignment_data: Assignment metadata to display in switch attributes
        """
        from ..const import DOMAIN

        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
            switches = entry_data.get("switches", {})
            switch = switches.get(area_id)
            if switch and hasattr(switch, "update_rule_data"):
                switch.update_rule_data(assignment_data)
                _LOGGER.debug(f"Updated switch attributes for {area_id}")
        except Exception as err:
            _LOGGER.debug(f"Could not update switch attributes for {area_id}: {err}")

    def _update_switch_last_action(self, area_id: str) -> None:
        """
        Update switch entity with last action info.

        Args:
            area_id: Area ID
        """
        from ..const import DOMAIN

        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
            switches = entry_data.get("switches", {})
            switch = switches.get(area_id)
            if switch and hasattr(switch, "update_last_action"):
                last_action = self._last_actions.get(area_id)
                if last_action:
                    switch.update_last_action(last_action)
                    _LOGGER.debug(f"Updated last action for {area_id}")
        except Exception as err:
            _LOGGER.debug(f"Could not update switch last action for {area_id}: {err}")
