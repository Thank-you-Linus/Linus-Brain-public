"""
Activity Tracker for Linus Brain

This module tracks activity states per area based on dynamic activity definitions.
Activities are loaded from AppStorage and evaluated using detection_conditions.

Activity detection:
- Each activity has detection_conditions (evaluated by ConditionEvaluator)
- Conditions are checked when presence sensors update
- Activity states have configurable thresholds and timeouts
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import ACTIVITY_EMPTY

_LOGGER = logging.getLogger(__name__)


class ActivityTracker:
    """
    Tracks activity levels in areas based on dynamic activity definitions.

    This class maintains state for each area and determines the current
    activity level by evaluating detection_conditions from AppStorage.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        app_storage=None,
        condition_evaluator=None,
    ) -> None:
        """
        Initialize the activity tracker.

        Args:
            hass: Home Assistant instance
            app_storage: AppStorage instance for activity definitions
            condition_evaluator: ConditionEvaluator for evaluating detection_conditions
        """
        self.hass = hass
        self.app_storage = app_storage
        self.condition_evaluator = condition_evaluator
        self._area_states: dict[str, dict[str, Any]] = {}
        self._activities: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._conditions_false_since: dict[str, datetime] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self.coordinator: Any = None

    async def async_initialize(self, force_reload: bool = False) -> None:
        """
        Initialize activity tracker by loading activities from storage.

        Loads activity definitions from AppStorage, including:
        - activity_id
        - detection_conditions (JSONB)
        - duration_threshold_seconds
        - timeout_seconds

        Args:
            force_reload: Force reload activities even if already initialized
        """
        if self._initialized and not force_reload:
            _LOGGER.debug("ActivityTracker already initialized")
            return

        if not self.app_storage:
            _LOGGER.warning(
                "No AppStorage provided, using fallback 'empty' activity only"
            )
            self._activities = {
                ACTIVITY_EMPTY: {
                    "activity_id": ACTIVITY_EMPTY,
                    "detection_conditions": [],
                    "duration_threshold_seconds": 0,
                    "timeout_seconds": 0,
                }
            }
        else:
            activities = self.app_storage.get_activities()

            if not activities:
                _LOGGER.warning("No activities found in storage, using empty only")
                activities = {
                    ACTIVITY_EMPTY: {
                        "activity_id": ACTIVITY_EMPTY,
                        "detection_conditions": [],
                        "duration_threshold_seconds": 0,
                        "timeout_seconds": 0,
                    }
                }

            self._activities = activities
            _LOGGER.info(
                f"Loaded {len(self._activities)} activities: {list(self._activities.keys())}"
            )

        self._initialized = True

    async def async_reload_activities(self) -> bool:
        """
        Reload activity definitions from AppStorage.

        This is useful when activity configurations (like timeouts) are updated
        from the cloud without restarting HA.

        Returns:
            True if reload was successful, False otherwise
        """
        if not self.app_storage:
            _LOGGER.warning("No AppStorage available, cannot reload activities")
            return False

        try:
            activities = self.app_storage.get_activities()

            if not activities:
                _LOGGER.warning("No activities found after reload")
                return False

            old_count = len(self._activities)
            self._activities = activities

            _LOGGER.info(
                f"Reloaded {len(self._activities)} activities (was {old_count}): {list(self._activities.keys())}"
            )

            # Log timeout changes for debugging
            for activity_id, activity_data in self._activities.items():
                timeout = activity_data.get("timeout_seconds", 0)
                _LOGGER.debug(
                    f"Activity '{activity_id}' timeout: {timeout}s"
                )

            return True

        except Exception as err:
            _LOGGER.error(f"Failed to reload activities: {err}")
            return False

    def _schedule_timeout(self, area_id: str, timeout_seconds: float) -> None:
        """
        Schedule a timeout task to expire activity after timeout_seconds.

        Args:
            area_id: The area ID
            timeout_seconds: Timeout duration in seconds
        """
        self._cancel_timeout(area_id)

        task = asyncio.create_task(self._timeout_handler(area_id, timeout_seconds))
        self._timeout_tasks[area_id] = task
        current_activity = self._area_states.get(area_id, {}).get("activity", "unknown")
        _LOGGER.debug(
            f"Scheduled timeout for {area_id} in {timeout_seconds}s (current: {current_activity})"
        )

    def _cancel_timeout(self, area_id: str) -> None:
        """
        Cancel any pending timeout task for an area.

        Args:
            area_id: The area ID
        """
        if area_id in self._timeout_tasks:
            task = self._timeout_tasks[area_id]
            if not task.done():
                task.cancel()
                current_activity = self._area_states.get(area_id, {}).get(
                    "activity", "unknown"
                )
                _LOGGER.debug(
                    f"Cancelled timeout for {area_id} (current: {current_activity})"
                )
            del self._timeout_tasks[area_id]

    def _get_next_activity(self, current_activity_id: str) -> str | None:
        """
        Get the next activity in the transition chain.

        Args:
            current_activity_id: The current activity ID

        Returns:
            Next activity ID from transition_to, or None if no transition
        """
        if current_activity_id not in self._activities:
            return None

        activity_data = self._activities[current_activity_id]
        return activity_data.get("transition_to")

    async def _timeout_handler(self, area_id: str, timeout_seconds: float) -> None:
        """
        Handle timeout expiration for an area with transition support.

        When a timeout expires, checks if there's a transition_to activity.
        If yes, transitions to that activity. If no, falls back to empty.

        Args:
            area_id: The area ID
            timeout_seconds: Timeout duration in seconds
        """
        try:
            await asyncio.sleep(timeout_seconds)

            if area_id not in self._area_states:
                _LOGGER.warning(f"Timeout expired but no state for {area_id}")
                return

            current_activity = self._area_states[area_id].get("activity")
            if not current_activity or not isinstance(current_activity, str):
                _LOGGER.warning(f"Invalid activity state for {area_id}")
                return

            next_activity = self._get_next_activity(current_activity)

            if next_activity:
                _LOGGER.info(
                    f"Timeout expired for {area_id}: {current_activity} → {next_activity}"
                )
                now = datetime.now().astimezone()
                self._area_states[area_id]["activity"] = next_activity
                self._area_states[area_id]["activity_start"] = now
                self._area_states[area_id]["last_update"] = now

                if area_id in self._conditions_false_since:
                    del self._conditions_false_since[area_id]

                next_activity_data = self._activities.get(next_activity, {})
                next_timeout = next_activity_data.get("timeout_seconds", 0)
                if next_timeout > 0:
                    self._schedule_timeout(area_id, next_timeout)

                if self.coordinator:
                    await self.coordinator.async_send_area_update(area_id)
            else:
                _LOGGER.info(f"Timeout expired for {area_id}, no transition defined")

                if self.coordinator:
                    await self.coordinator.async_send_area_update(area_id)
                else:
                    _LOGGER.warning(
                        f"No coordinator available to trigger update for {area_id}"
                    )

        except asyncio.CancelledError:
            _LOGGER.debug(f"Timeout cancelled for {area_id}")
        except Exception as err:
            _LOGGER.error(f"Error in timeout handler for {area_id}: {err}")

    async def async_evaluate_activity(self, area_id: str) -> str:
        """
        Evaluate all activities for an area and return highest matching activity.

        Activities are evaluated in priority order (based on detection complexity).
        The first activity with matching conditions is returned.

        Args:
            area_id: The area ID

        Returns:
            Current activity level (activity_id from definitions)
        """
        if not self._initialized:
            await self.async_initialize()

        if not self.condition_evaluator:
            _LOGGER.warning(
                "No ConditionEvaluator provided, cannot evaluate activities"
            )
            return ACTIVITY_EMPTY

        if area_id in self._area_states:
            current_activity = self._area_states[area_id].get("activity")
            if current_activity and current_activity != ACTIVITY_EMPTY:
                activity_data = self._activities.get(current_activity, {})
                timeout = activity_data.get("timeout_seconds", 0)
                if timeout > 0 and area_id in self._conditions_false_since:
                    false_since = self._conditions_false_since[area_id]
                    elapsed = (
                        datetime.now().astimezone() - false_since
                    ).total_seconds()
                    if elapsed >= timeout:
                        _LOGGER.info(
                            f"Area {area_id}: {current_activity} timed out after {elapsed:.1f}s (timeout: {timeout}s)"
                        )
                        now = datetime.now().astimezone()
                        self._area_states[area_id]["activity"] = ACTIVITY_EMPTY
                        self._area_states[area_id]["activity_start"] = None
                        self._area_states[area_id]["last_update"] = now
                        del self._conditions_false_since[area_id]
                        return ACTIVITY_EMPTY

        _LOGGER.debug(
            f"Evaluating activities for area {area_id}: {list(self._activities.keys())}"
        )

        for activity_id, activity_data in self._activities.items():
            if activity_id == ACTIVITY_EMPTY:
                continue

            is_transition_state = activity_data.get("is_transition_state", False)
            if is_transition_state:
                _LOGGER.debug(
                    f"Activity {activity_id}: is transition state, skipping direct evaluation"
                )
                continue

            conditions = activity_data.get("detection_conditions", [])

            if not conditions:
                _LOGGER.debug(
                    f"Activity {activity_id}: no conditions defined, skipping"
                )
                continue

            _LOGGER.debug(f"Activity {activity_id}: checking conditions {conditions}")

            try:
                is_match = await self.condition_evaluator.evaluate_conditions(
                    conditions, area_id, logic="and"
                )

                _LOGGER.debug(
                    f"Activity {activity_id}: condition evaluation result = {is_match}"
                )

                if is_match:
                    duration_threshold = activity_data.get(
                        "duration_threshold_seconds", 0
                    )

                    if duration_threshold > 0:
                        if area_id not in self._area_states:
                            self._area_states[area_id] = {
                                "activity": ACTIVITY_EMPTY,
                                "activity_start": None,
                                "last_update": None,
                            }

                        state = self._area_states[area_id]
                        now = datetime.now().astimezone()

                        if state.get("activity") != activity_id:
                            state["activity_start"] = now
                            state["activity"] = activity_id
                            state["last_update"] = now
                            _LOGGER.debug(
                                f"Area {area_id}: started {activity_id} (threshold: {duration_threshold}s)"
                            )
                            return ACTIVITY_EMPTY

                        duration = (now - state["activity_start"]).total_seconds()

                        if duration >= duration_threshold:
                            _LOGGER.debug(
                                f"Area {area_id}: {activity_id} threshold met ({duration:.1f}s)"
                            )
                            return activity_id
                        else:
                            _LOGGER.debug(
                                f"Area {area_id}: {activity_id} in progress ({duration:.1f}s/{duration_threshold}s)"
                            )
                            return ACTIVITY_EMPTY
                    else:
                        if area_id not in self._area_states:
                            self._area_states[area_id] = {
                                "activity": activity_id,
                                "activity_start": datetime.now().astimezone(),
                                "last_update": datetime.now().astimezone(),
                            }
                        else:
                            now = datetime.now().astimezone()
                            state = self._area_states[area_id]
                            if state.get("activity") != activity_id:
                                state["activity_start"] = now
                            state["activity"] = activity_id
                            state["last_update"] = now

                        if area_id in self._conditions_false_since:
                            del self._conditions_false_since[area_id]
                        self._cancel_timeout(area_id)

                        _LOGGER.debug(
                            f"Area {area_id}: {activity_id} detected (no threshold)"
                        )
                        return activity_id

            except Exception as err:
                _LOGGER.error(
                    f"Failed to evaluate activity {activity_id} for area {area_id}: {err}"
                )
                continue

        if area_id in self._area_states:
            now = datetime.now().astimezone()
            state = self._area_states[area_id]
            current_activity_val = state.get("activity")
            current_activity = (
                current_activity_val
                if isinstance(current_activity_val, str)
                else ACTIVITY_EMPTY
            )

            if current_activity != ACTIVITY_EMPTY:
                activity_data = self._activities.get(current_activity, {})
                transition_to = activity_data.get("transition_to")
                is_transition_state = activity_data.get("is_transition_state", False)

                # Don't process timeout logic for transition states when conditions are false
                # Transition states should only be entered via timeout, not direct evaluation
                # IMPORTANT: We do NOT cancel existing timeouts - they should complete naturally
                if is_transition_state:
                    _LOGGER.debug(
                        f"Area {area_id}: {current_activity} is transition state, allowing existing timeout to complete"
                    )
                    if area_id in self._conditions_false_since:
                        del self._conditions_false_since[area_id]
                    # DO NOT cancel timeout - let it complete naturally to allow transition
                elif area_id not in self._conditions_false_since:
                    self._conditions_false_since[area_id] = now

                    if transition_to:
                        timeout_val = activity_data.get("timeout_seconds", 0)
                        timeout = float(timeout_val) if timeout_val else 0

                        if timeout > 0:
                            _LOGGER.info(
                                f"Area {area_id}: {current_activity} conditions no longer match, will transition to {transition_to} after {timeout}s"
                            )
                            self._schedule_timeout(area_id, timeout)
                        else:
                            _LOGGER.info(
                                f"Area {area_id}: {current_activity} conditions no longer match, immediately transitioning to {transition_to}"
                            )
                            state["activity"] = transition_to
                            state["activity_start"] = now
                            state["last_update"] = now
                            del self._conditions_false_since[area_id]

                            transition_data = self._activities.get(transition_to, {})
                            transition_timeout = transition_data.get(
                                "timeout_seconds", 0
                            )
                            if transition_timeout > 0:
                                self._schedule_timeout(area_id, transition_timeout)

                            if self.coordinator:
                                await self.coordinator.async_send_area_update(area_id)
                    else:
                        _LOGGER.debug(
                            f"Area {area_id}: {current_activity} conditions no longer match, timeout started"
                        )
                        timeout_val = activity_data.get("timeout_seconds", 0)
                        timeout = float(timeout_val) if timeout_val else 0
                        if timeout > 0:
                            self._schedule_timeout(area_id, timeout)
            else:
                if area_id in self._conditions_false_since:
                    _LOGGER.debug(
                        f"Area {area_id}: {current_activity} conditions match again, clearing conditions_false_since"
                    )
                    del self._conditions_false_since[area_id]
                _LOGGER.debug(
                    f"Area {area_id}: {current_activity} conditions match, cancelling timeout"
                )
                self._cancel_timeout(area_id)

        return self.get_activity(area_id)

    def update_presence(self, area_id: str, has_presence: bool) -> str:
        """
        Legacy method for backward compatibility.

        This method is deprecated and will trigger dynamic activity evaluation.
        Use async_evaluate_activity() directly for new code.

        Args:
            area_id: The area ID
            has_presence: Whether presence is currently detected (ignored)

        Returns:
            Current activity level
        """
        _LOGGER.warning(
            "update_presence() is deprecated. Use async_evaluate_activity() instead."
        )

        if area_id not in self._area_states:
            self._area_states[area_id] = {
                "activity": ACTIVITY_EMPTY,
                "activity_start": None,
                "last_update": None,
            }

        return self._area_states[area_id]["activity"]

    def get_activity(self, area_id: str) -> str:
        """
        Get current activity level for an area.

        Args:
            area_id: The area ID

        Returns:
            Current activity level (empty, movement, occupied)
        """
        if area_id not in self._area_states:
            return ACTIVITY_EMPTY
        return self._area_states[area_id]["activity"]

    async def get_activity_level(self, area_id: str) -> str:
        """
        Get current activity level for an area (async version).

        Args:
            area_id: The area ID

        Returns:
            Current activity level (empty, movement, occupied)
        """
        return self.get_activity(area_id)

    def get_activity_duration(self, area_id: str) -> float:
        """
        Get duration of current activity in seconds.

        Args:
            area_id: The area ID

        Returns:
            Duration in seconds, or 0 if no activity
        """
        if area_id not in self._area_states:
            return 0.0

        state = self._area_states[area_id]
        if not state.get("activity_start"):
            return 0.0

        duration = (
            datetime.now().astimezone() - state["activity_start"]
        ).total_seconds()
        return duration

    def get_time_until_state_loss(self, area_id: str) -> float | None:
        """
        Get time remaining before activity state automatically times out.

        Args:
            area_id: The area ID

        Returns:
            Seconds remaining before timeout, or None if no timeout configured
        """
        if area_id not in self._area_states:
            return None

        state = self._area_states[area_id]
        current_activity_id = state.get("activity")

        if not current_activity_id or current_activity_id == ACTIVITY_EMPTY:
            return None

        activity_data = self._activities.get(current_activity_id)
        if not activity_data:
            return None

        timeout_seconds = activity_data.get("timeout_seconds", 0)
        if timeout_seconds == 0:
            return None

        last_update = state.get("last_update")
        if not last_update:
            return None

        time_since_update = (datetime.now().astimezone() - last_update).total_seconds()
        remaining = timeout_seconds - time_since_update

        return max(0, remaining) if remaining > 0 else None

    def get_all_activities(self) -> dict[str, dict[str, Any]]:
        """
        Get all area activities.

        Returns:
            Dictionary mapping area_id to activity state
        """
        result = {}
        for area_id, state in self._area_states.items():
            result[area_id] = {
                "activity": state["activity"],
                "duration": self.get_activity_duration(area_id),
            }
        return result

    def reset_area(self, area_id: str) -> None:
        """
        Reset activity tracking for an area.

        Args:
            area_id: The area ID to reset
        """
        if area_id in self._area_states:
            del self._area_states[area_id]
            _LOGGER.debug(f"Reset activity tracking for area {area_id}")

    async def simulate_activity(
        self, area_id: str, activity: str, duration: int = 0
    ) -> None:
        """
        Override activity level for testing purposes.

        This method allows testing automation rules by simulating activity states
        without requiring actual presence sensors. If duration is provided, the
        activity will automatically reset after the specified time.

        Args:
            area_id: The area ID to simulate activity for
            activity: Activity ID from loaded activities
            duration: Optional duration in seconds before auto-reset (0 = no reset)
        """
        if activity not in self._activities:
            _LOGGER.error(
                f"Invalid activity: {activity}. Available: {list(self._activities.keys())}"
            )
            return

        now = datetime.now().astimezone()

        self._area_states[area_id] = {
            "activity": activity,
            "activity_start": now,
            "last_update": now,
            "_simulated": True,
        }

        _LOGGER.info(
            f"Simulated {activity} activity for area {area_id}"
            + (f" (auto-reset in {duration}s)" if duration > 0 else "")
        )

        if duration > 0:
            import asyncio

            await asyncio.sleep(duration)
            self.reset_area(area_id)
            _LOGGER.info(f"Auto-reset activity for area {area_id} after {duration}s")
