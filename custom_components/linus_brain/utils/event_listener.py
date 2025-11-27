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

from .state_validator import is_state_valid
from .timeout_manager import TimeoutManager

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

        # Use TimeoutManager for debouncing area updates
        self._debounce_manager = TimeoutManager(
            logger=_LOGGER, logger_prefix="[DEBOUNCE]"
        )

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
        from homeassistant.helpers import entity_registry as er
        from .area_manager import get_monitored_domains

        domain = split_entity_id(entity_id)[0]

        # IMPORTANT: Ignore Linus Brain's own entities to prevent feedback loops
        # Our sensors (context, insights, stats, etc.) should not trigger area updates
        if entity_id.startswith("sensor.linus_brain_") or entity_id.startswith(
            "switch.linus_brain_"
        ):
            return False

        # Get dynamic monitored domains (includes base + activity detection_conditions)
        monitored_domains = get_monitored_domains()

        # Check if domain is monitored
        if domain not in monitored_domains:
            # Special debug for occupancy sensors
            if "occupancy" in entity_id or (state.attributes.get("device_class") == "occupancy"):
                _LOGGER.error(
                    f"🚨 {entity_id}: domain '{domain}' NOT in monitored_domains! "
                    f"Available domains: {list(monitored_domains.keys())}"
                )
            return False

        # For media_player and light, always process
        if domain in ("media_player", "light"):
            _LOGGER.debug(f"✅ Will process {entity_id}: domain={domain} always monitored")
            return True

        # Try to get device_class from state attributes first, then from entity_registry
        device_class = state.attributes.get("original_device_class") or state.attributes.get("device_class")
        
        # If no device_class in state, check entity_registry (for entities with original_device_class set)
        if not device_class:
            ent_reg = er.async_get(self.hass)
            entity_entry = ent_reg.async_get(entity_id)
            if entity_entry:
                device_class = entity_entry.original_device_class or entity_entry.device_class

        # Special debug for occupancy sensors
        if device_class == "occupancy":
            monitored_classes = monitored_domains.get(domain, [])
            _LOGGER.info(
                f"🔍 Checking occupancy sensor {entity_id}: "
                f"domain={domain}, device_class={device_class}, "
                f"monitored_classes={monitored_classes}, "
                f"will_process={device_class in MONITORED_DEVICE_CLASSES}"
            )

        if device_class in MONITORED_DEVICE_CLASSES:
            return True

        return False

    async def _deferred_area_update(self, area: str) -> None:
        """
        Execute a deferred update for an area.

        Args:
            area: The area ID
        """
        _LOGGER.debug(f"Executing deferred update for area {area}")
        await self.coordinator.async_send_area_update(area)

    def _should_debounce(self, area: str, entity_id: str, new_state: State) -> bool:
        """
        Check if an update for an area should be debounced.

        If debouncing is needed, schedules a deferred update that will be automatically
        cancelled and rescheduled if more events arrive.

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

        # Skip invalid states
        if not is_state_valid(new_state):
            _LOGGER.debug(
                f"Skipping debounce check for {entity_id} with invalid state: {new_state.state}"
            )
            return True  # Debounce (skip) invalid states

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
                    f"Sensor {entity_id} OFF, bypassing debounce"
                )
                self._last_update_times[area] = time.time()
                self._debounce_manager.cancel(area)
                return False

            if new_state.state == "on":
                current_activity = self.coordinator.get_area_activity(area)
                if current_activity == "inactive":
                    _LOGGER.debug(
                        f"Sensor {entity_id} ON while inactive, bypassing debounce"
                    )
                    self._last_update_times[area] = time.time()
                    self._debounce_manager.cancel(area)
                    return False

        current_time = time.time()
        last_update = self._last_update_times.get(area, 0)

        if current_time - last_update < self._debounce_interval:
            # Schedule deferred update using TimeoutManager
            # This automatically cancels and replaces any existing pending update
            _LOGGER.debug(f"Scheduling deferred update for area {area}")
            self._debounce_manager.schedule(
                key=area,
                delay=self._debounce_interval,
                callback=self._deferred_area_update,
                area=area,
            )
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

        # Ignore transitions FROM invalid states (startup restoration)
        # This prevents triggering rules when HA restores states from storage
        if old_state and not is_state_valid(old_state):
            _LOGGER.debug(
                f"⏭️ Ignoring {entity_id}: transition from invalid state "
                f"({old_state.state} -> {new_state.state})"
            )
            return

        # Log binary_sensor occupancy events for debugging
        domain = split_entity_id(entity_id)[0]
        if domain == "binary_sensor":
            device_class = new_state.attributes.get("original_device_class") or new_state.attributes.get("device_class")
            if device_class == "occupancy":
                _LOGGER.info(
                    f"👁️ Received binary_sensor.occupancy event: {entity_id} "
                    f"({old_state.state if old_state else 'unknown'} -> {new_state.state})"
                )

        if not self._should_process_entity(entity_id, new_state):
            return

        if old_state and old_state.state == new_state.state:
            return

        _LOGGER.debug(
            f"State changed for {entity_id}: {old_state.state if old_state else 'unknown'} -> {new_state.state}"
        )

        # Log media_player events for debugging
        if domain == "media_player":
            _LOGGER.debug(
                f"🎵 Received media_player event: {entity_id} "
                f"({old_state.state if old_state else 'unknown'} -> {new_state.state})"
            )

        if not self._should_process_entity(entity_id, new_state):
            _LOGGER.debug(f"⛔ Entity {entity_id} not processed by _should_process_entity")
            return

        if old_state and old_state.state == new_state.state:
            _LOGGER.debug(f"⛔ Entity {entity_id} state unchanged: {new_state.state}")
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
            # Get device class for better logging
            device_class = None
            if new_state and new_state.attributes:
                device_class = new_state.attributes.get("device_class") or new_state.attributes.get("original_device_class")

            # Get entity and device info for debugging
            from homeassistant.helpers import entity_registry as er, device_registry as dr
            ent_reg = er.async_get(self.hass)
            entity_entry = ent_reg.async_get(entity_id)
            
            debug_info = ""
            if entity_entry:
                debug_info = f"\n   entity.area_id={entity_entry.area_id}"
                debug_info += f"\n   entity.device_id={entity_entry.device_id}"
                debug_info += f"\n   entity.original_device_class={entity_entry.original_device_class}"
                
                if entity_entry.device_id:
                    dev_reg = dr.async_get(self.hass)
                    device = dev_reg.async_get(entity_entry.device_id)
                    if device:
                        debug_info += f"\n   device.area_id={device.area_id}"
                        debug_info += f"\n   device.name={device.name_by_user or device.name}"

            _LOGGER.warning(
                f"⚠️ Entity {entity_id} (device_class={device_class}) has no associated area, skipping. "
                f"Please assign this entity or its device to an area in Home Assistant.{debug_info}"
            )
            return

        # Get device class for logging
        device_class = new_state.attributes.get("device_class")

        if self._should_debounce(area, entity_id, new_state):
            _LOGGER.debug(
                f"⏱️ Debouncing update for area {area} from {entity_id}"
            )
            return

        _LOGGER.info(
            f"✅ Triggering update for area {area} from {entity_id} "
            f"(domain={domain}, device_class={device_class}, state={new_state.state})"
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

        # Log all entities that would be monitored
        _LOGGER.info("📋 Scanning entities that will be monitored by EventListener...")
        
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import area_registry as ar
        from .area_manager import get_monitored_domains
        
        monitored_domains = get_monitored_domains()
        _LOGGER.info(f"💡 Monitored domains: {monitored_domains}")
        
        ent_reg = er.async_get(self.hass)
        
        monitored_entities = []
        ignored_entities = []
        rejected_occupancy_sensors = []
        all_binary_sensors_in_garage = []
        
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            
            # Skip Linus Brain's own entities
            if entity_id.startswith("sensor.linus_brain_") or entity_id.startswith("switch.linus_brain_"):
                continue
                
            domain = split_entity_id(entity_id)[0]
            
            # Get device_class from state attributes OR entity_registry
            device_class = state.attributes.get("original_device_class") or state.attributes.get("device_class")
            if not device_class:
                entity_entry = ent_reg.async_get(entity_id)
                if entity_entry:
                    device_class = entity_entry.original_device_class or entity_entry.device_class
            
            # Debug: Track all binary_sensors in garage area
            if domain == "binary_sensor":
                area = self.coordinator.area_manager.get_entity_area(entity_id)
                entity_entry = ent_reg.async_get(entity_id)
                
                # Log ALL binary_sensors with occupancy device_class (regardless of area)
                if device_class == "occupancy":
                    _LOGGER.warning(f"🔍 Found binary_sensor with occupancy: {entity_id}")
                    _LOGGER.warning(f"    area from get_entity_area: {area}")
                    _LOGGER.warning(f"    entity.area_id: {entity_entry.area_id if entity_entry else 'NO ENTRY'}")
                    _LOGGER.warning(f"    entity.device_id: {entity_entry.device_id if entity_entry else 'NO ENTRY'}")
                    _LOGGER.warning(f"    entity.original_device_class: {entity_entry.original_device_class if entity_entry else 'NO ENTRY'}")
                    _LOGGER.warning(f"    entity.device_class: {entity_entry.device_class if entity_entry else 'NO ENTRY'}")
                    
                    if entity_entry and entity_entry.device_id:
                        from homeassistant.helpers import device_registry as dr
                        dev_reg = dr.async_get(self.hass)
                        device = dev_reg.async_get(entity_entry.device_id)
                        if device:
                            _LOGGER.warning(f"    device.area_id: {device.area_id if device else 'NO DEVICE'}")
                            _LOGGER.warning(f"    device.name: {device.name_by_user or device.name if device else 'NO DEVICE'}")
                        else:
                            _LOGGER.warning(f"    device: NOT FOUND (device_id={entity_entry.device_id})")
                    
                    # Check if would be monitored
                    will_be_monitored = self._should_process_entity(entity_id, state)
                    _LOGGER.warning(f"    will_be_monitored: {will_be_monitored}")
                    
                    if not will_be_monitored:
                        _LOGGER.error(f"❌ {entity_id} will NOT be monitored! Investigating why...")
                        _LOGGER.error(f"    domain '{domain}' in monitored_domains? {domain in monitored_domains}")
                        if domain in monitored_domains:
                            _LOGGER.error(f"    monitored device_classes for {domain}: {monitored_domains[domain]}")
                            _LOGGER.error(f"    entity device_class '{device_class}' in list? {device_class in monitored_domains[domain] if monitored_domains[domain] else 'N/A (empty list = all)'}")
                    else:
                        if not area:
                            _LOGGER.error(f"⚠️ {entity_id} WOULD be monitored BUT has no area!")
                        else:
                            _LOGGER.warning(f"✅ {entity_id} WILL be monitored for area '{area}'")
                
                if area == "garage":
                    all_binary_sensors_in_garage.append({
                        "entity_id": entity_id,
                        "device_class_from_state": state.attributes.get("device_class"),
                        "original_device_class_from_state": state.attributes.get("original_device_class"),
                        "device_class_from_registry": entity_entry.device_class if entity_entry else None,
                        "original_device_class_from_registry": entity_entry.original_device_class if entity_entry else None,
                        "final_device_class": device_class,
                        "state": state.state,
                    })
            
            # Track occupancy sensors that are rejected
            if domain == "binary_sensor" and device_class == "occupancy":
                if domain not in monitored_domains:
                    rejected_occupancy_sensors.append({
                        "entity_id": entity_id,
                        "reason": f"domain '{domain}' not in monitored_domains",
                        "monitored_domains": list(monitored_domains.keys())
                    })
                    continue
            
            # Check if domain is monitored
            if domain not in monitored_domains:
                continue
            
            # Check if would be processed
            if self._should_process_entity(entity_id, state):
                area = self.coordinator.area_manager.get_entity_area(entity_id)
                monitored_entities.append({
                    "entity_id": entity_id,
                    "domain": domain,
                    "device_class": device_class,
                    "area": area,
                    "state": state.state
                })
            else:
                device_class = state.attributes.get("original_device_class") or state.attributes.get("device_class")
                area = self.coordinator.area_manager.get_entity_area(entity_id)
                ignored_entities.append({
                    "entity_id": entity_id,
                    "domain": domain,
                    "device_class": device_class,
                    "area": area,
                    "reason": "No area assigned" if not area else f"Device class {device_class} not monitored"
                })
        
        # Log summary
        _LOGGER.info(f"✅ EventListener will monitor {len(monitored_entities)} entities:")
        
        # Group by area for better readability
        by_area = {}
        for entity_info in monitored_entities:
            area = entity_info["area"] or "no_area"
            if area not in by_area:
                by_area[area] = []
            by_area[area].append(entity_info)
        
        for area, entities in sorted(by_area.items()):
            if area == "no_area":
                continue
            _LOGGER.info(f"  Area '{area}': {len(entities)} entities")
            for e in entities:
                _LOGGER.info(f"    - {e['entity_id']} (domain={e['domain']}, device_class={e['device_class']}, state={e['state']})")
        
        # Log entities without area
        if "no_area" in by_area:
            _LOGGER.warning(f"⚠️ {len(by_area['no_area'])} monitored entities have NO AREA assigned (will be ignored):")
            for e in by_area["no_area"]:
                _LOGGER.warning(f"    - {e['entity_id']} (domain={e['domain']}, device_class={e['device_class']})")
        
        # Log ignored entities (for debugging)
        if ignored_entities:
            _LOGGER.debug(f"❌ {len(ignored_entities)} entities in monitored domains but ignored:")
            for e in ignored_entities[:10]:  # Limit to first 10
                _LOGGER.debug(f"    - {e['entity_id']}: {e['reason']}")
        
        # Log rejected occupancy sensors specifically
        if rejected_occupancy_sensors:
            _LOGGER.error(f"🚨 {len(rejected_occupancy_sensors)} binary_sensor.occupancy were REJECTED:")
            for e in rejected_occupancy_sensors:
                _LOGGER.error(f"    - {e['entity_id']}: {e['reason']}")
                _LOGGER.error(f"      Monitored domains: {e['monitored_domains']}")
        
        # Debug: Log all binary_sensors in garage
        if all_binary_sensors_in_garage:
            _LOGGER.warning(f"🔧 DEBUG: Found {len(all_binary_sensors_in_garage)} binary_sensors in 'garage' area:")
            for e in all_binary_sensors_in_garage:
                _LOGGER.warning(f"    - {e['entity_id']}")
                _LOGGER.warning(f"      device_class_from_state: {e['device_class_from_state']}")
                _LOGGER.warning(f"      original_device_class_from_state: {e['original_device_class_from_state']}")
                _LOGGER.warning(f"      device_class_from_registry: {e['device_class_from_registry']}")
                _LOGGER.warning(f"      original_device_class_from_registry: {e['original_device_class_from_registry']}")
                _LOGGER.warning(f"      final_device_class: {e['final_device_class']}")
                _LOGGER.warning(f"      state: {e['state']}")
        else:
            _LOGGER.warning("🔧 DEBUG: No binary_sensors found in 'garage' area - check area assignments!")

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
        cancelled_count = self._debounce_manager.cancel_all()
        if cancelled_count > 0:
            _LOGGER.debug(f"Cancelled {cancelled_count} pending debounced updates")

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
