"""
Event Listener for Linus Brain

This module listens to Home Assistant state changes for relevant entities
and triggers immediate updates to Supabase when changes occur.

Key responsibilities:
- Register state change listeners for monitored entities
- Filter events by domain and device class
- Trigger coordinator updates for affected areas
- Handle listener lifecycle (start/stop)
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback, split_entity_id

if TYPE_CHECKING:
    from .light_learning import LightLearning

_LOGGER = logging.getLogger(__name__)

# Device classes to monitor (for binary_sensor and sensor)
MONITORED_DEVICE_CLASSES = ["motion", "presence", "occupancy", "illuminance"]


class EventListener:
    """
    Listens to entity state changes and triggers updates.

    This class:
    - Monitors state changes for relevant entities
    - Determines which area is affected
    - Triggers immediate data sync for that area
    - Prevents unnecessary updates (debouncing)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: Any,
        light_learning: "LightLearning | None" = None,
    ) -> None:
        """
        Initialize the event listener.

        Args:
            hass: Home Assistant instance
            coordinator: LinusBrainCoordinator instance
            light_learning: Optional LightLearning instance for capturing manual light actions
        """
        self.hass = hass
        self.coordinator = coordinator
        self.light_learning = light_learning
        self._listeners: list[Callable[[], None]] = []
        self._last_update_times: dict[str, float] = {}
        self._pending_updates: dict[str, asyncio.Task[None]] = {}

        self._debounce_interval = 5.0

    def _should_process_entity(self, entity_id: str, state: State) -> bool:
        """
        Determine if an entity should be processed based on domain and device class.

        Args:
            entity_id: The entity ID
            state: The entity's state object

        Returns:
            True if entity should be processed, False otherwise
        """
        from .area_manager import get_monitored_domains

        domain = split_entity_id(entity_id)[0]

        # Get dynamic monitored domains (includes base + activity detection_conditions)
        monitored_domains = get_monitored_domains()
        
        # Check if domain is monitored
        if domain not in monitored_domains:
            return False

        # For media_player and light, always process
        if domain in ("media_player", "light"):
            return True

        # Try original_device_class first, then device_class
        device_class = state.attributes.get(
            "original_device_class"
        ) or state.attributes.get("device_class")
        if device_class in MONITORED_DEVICE_CLASSES:
            return True

        return False

    async def _schedule_deferred_update(self, area: str) -> None:
        """
        Schedule a deferred update for an area after debounce interval.

        Args:
            area: The area ID
        """
        await asyncio.sleep(self._debounce_interval)

        if area in self._pending_updates:
            del self._pending_updates[area]

        _LOGGER.debug(f"Executing deferred update for area {area}")
        await self.coordinator.async_send_area_update(area)

    def _should_debounce(self, area: str, entity_id: str, new_state: State) -> bool:
        """
        Check if an update for an area should be debounced.

        If debouncing is needed, schedules a deferred update instead of dropping it.

        Special handling: Motion/presence sensors turning OFF are never debounced,
        as this is a critical event that triggers activity transitions.

        Args:
            area: The area ID
            entity_id: The entity that changed
            new_state: The new state of the entity

        Returns:
            True if update should be deferred (debounced), False if should process now
        """
        import time

        domain = split_entity_id(entity_id)[0]
        device_class = new_state.attributes.get(
            "original_device_class"
        ) or new_state.attributes.get("device_class")

        if domain == "binary_sensor" and device_class in (
            "motion",
            "presence",
            "occupancy",
        ):
            if new_state.state == "off":
                _LOGGER.debug(
                    f"Motion/presence sensor {entity_id} turned OFF, bypassing debounce for immediate transition"
                )
                self._last_update_times[area] = time.time()
                return False

            if new_state.state == "on":
                current_activity = self.coordinator.get_area_activity(area)
                if current_activity == "inactive":
                    _LOGGER.debug(
                        f"Motion/presence sensor {entity_id} turned ON while area is inactive, bypassing debounce for immediate transition"
                    )
                    self._last_update_times[area] = time.time()
                    return False

        current_time = time.time()
        last_update = self._last_update_times.get(area, 0)

        if current_time - last_update < self._debounce_interval:
            if area not in self._pending_updates:
                _LOGGER.debug(f"Scheduling deferred update for area {area}")
                task = self.hass.async_create_task(self._schedule_deferred_update(area))
                task.add_done_callback(self._handle_task_exception)
                self._pending_updates[area] = task
            return True

        self._last_update_times[area] = current_time
        return False

    @callback
    def _async_state_changed_listener(self, event: Event[Any]) -> None:
        """
        Handle state change events.

        This callback is invoked whenever a monitored entity changes state.
        It determines the affected area and triggers an update.

        Args:
            event: The state change event
        """
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if not new_state or not entity_id:
            return

        if not self._should_process_entity(entity_id, new_state):
            return

        if old_state and old_state.state == new_state.state:
            return

        _LOGGER.debug(
            f"State changed for {entity_id}: {old_state.state if old_state else 'unknown'} -> {new_state.state}"
        )

        domain = split_entity_id(entity_id)[0]

        if domain == "light" and self.light_learning:
            task = self.hass.async_create_task(
                self.light_learning.capture_light_action(
                    entity_id, new_state, old_state, event.context
                )
            )
            task.add_done_callback(self._handle_task_exception)
            return

        area = self.coordinator.area_manager.get_entity_area(entity_id)

        if not area:
            _LOGGER.debug(f"Entity {entity_id} has no associated area, skipping")
            return

        if self._should_debounce(area, entity_id, new_state):
            _LOGGER.debug(f"Debouncing update for area {area}")
            return

        _LOGGER.info(
            f"Triggering update for area {area} due to {entity_id} state change"
        )

        task = self.hass.async_create_task(
            self.coordinator.async_send_area_update(area)
        )
        task.add_done_callback(self._handle_task_exception)

    def _handle_task_exception(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            _LOGGER.debug("Task was cancelled")
        except Exception as err:
            _LOGGER.error(f"Task raised exception: {err}", exc_info=True)

    async def async_start_listening(self) -> None:
        """
        Start listening to state changes.

        This registers the event listener with Home Assistant's event bus.
        """
        _LOGGER.info("Starting event listener for Linus Brain")

        # Listen to all state changes and filter in the callback
        # This is more efficient than subscribing to individual entities
        remove_listener = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._async_state_changed_listener
        )

        self._listeners.append(remove_listener)

        _LOGGER.info("Event listener started successfully")

    async def async_stop_listening(self) -> None:
        """
        Stop listening to state changes.

        This is called during integration unload to clean up listeners.
        """
        _LOGGER.info("Stopping event listener for Linus Brain")

        # Remove all registered listeners
        for remove_listener in self._listeners:
            remove_listener()

        self._listeners.clear()
        self._last_update_times.clear()

        # Cancel any pending deferred updates
        for task in self._pending_updates.values():
            task.cancel()
        self._pending_updates.clear()

        _LOGGER.info("Event listener stopped successfully")

    def get_stats(self) -> dict[str, Any]:
        """
        Get statistics about the event listener.

        Returns:
            Dictionary with listener statistics
        """
        return {
            "active_listeners": len(self._listeners),
            "monitored_areas": len(self._last_update_times),
            "debounce_interval": self._debounce_interval,
        }
