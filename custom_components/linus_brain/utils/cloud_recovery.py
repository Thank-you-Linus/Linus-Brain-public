"""
Cloud recovery manager for Linus Brain.

This module owns the transition between degraded mode (Supabase unreachable) and
nominal mode. It periodically probes cloud availability and, on the
unavailable -> available edge, runs a single ordered recovery pass so the
integration returns to nominal mode without any manual intervention.

Key responsibilities:
- Own the availability latch (`_was_available`) and the re-entrancy lock, so a
  recovery pass runs exactly once per unavailable -> available edge
- Run the recovery sequence in a fixed order: instance identity -> apps and
  activities re-sync -> insights reload -> pending light-action replay
- Stop the sequence at the first failing step and leave the latch disarmed, so
  the next probe replays the whole sequence from the beginning
- Expose a synchronous, I/O-free observability snapshot (`get_status()`)
  consumed by the `sensor.linus_brain_cloud_health` diagnostic sensor
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# --- Transitional joints (tickets 02 / 03 / 04) ------------------------------
# This ticket CONSUMES three symbols owned by upstream tickets. Until those are
# merged the adapters below take a neutral path so this module imports and runs
# unchanged. They are joints, NOT fallback implementations: never write a second
# replay queue, a second availability state or a second insights cache here.
#
# - ticket 02: SupabaseClient.is_available() -- sole owner of availability state
# - ticket 03: InsightsManager.is_stale() / .loaded_from_cache -- provenance
# - ticket 04: the light-action buffer's single replay method plus a public,
#   synchronous, read-only pending counter, both on the LightLearning object
#
# When a ticket lands, delete its adapter body and call the symbol directly.
_IS_AVAILABLE_ATTR = "is_available"
_INSIGHTS_STALE_ATTR = "is_stale"
_INSIGHTS_FROM_CACHE_ATTR = "loaded_from_cache"
_REPLAY_METHOD_ATTR = "async_replay_pending_actions"
_PENDING_COUNT_ATTR = "get_pending_count"


class CloudRecoveryManager:
    """
    Detect the degraded -> nominal transition and restore nominal mode.

    The manager is the single owner of the recovery state: nothing else should
    track cloud availability, schedule a recovery pass, or decide that the
    integration is running in degraded mode.
    """

    def __init__(
        self,
        hass: Any,
        coordinator: Any,
        insights_manager: Any,
        light_learning: Any,
    ) -> None:
        """
        Initialize the cloud recovery manager.

        Args:
            hass: Home Assistant instance (used to list areas for the re-sync)
            coordinator: LinusBrainCoordinator owning the Supabase client
            insights_manager: InsightsManager to reload after a re-sync
            light_learning: LightLearning owning the pending light-action buffer
        """
        self.hass = hass
        self.coordinator = coordinator
        self.insights_manager = insights_manager
        self.light_learning = light_learning

        self._lock = asyncio.Lock()
        # Start armed: async_setup_entry has just performed a full load, so
        # startup is nominal. A recovery pass must only fire on a real
        # available -> unavailable -> available edge.
        self._was_available = True
        self._available = True
        self._last_success: str | None = None
        self._last_failed_step: str | None = None

    # -- Transitional adapters -------------------------------------------------

    async def _is_available(self) -> bool:
        """
        Return the cloud availability reported by the Supabase client.

        Neutral path (ticket 02 not merged): availability is assumed True, so no
        recovery pass is ever triggered.
        """
        client = getattr(self.coordinator, "supabase_client", None)
        probe = getattr(client, _IS_AVAILABLE_ATTR, None)
        if probe is None:
            return True
        if not callable(probe):
            # Ticket 02 may expose availability as a property or a plain
            # attribute rather than a method: read it instead of calling it.
            return bool(probe)
        result = probe()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    def _insights_provenance(self) -> dict[str, bool | None]:
        """
        Return the insights provenance as machine data.

        Neutral path (ticket 03 not merged): both values are None, meaning
        "unknown provenance" rather than a fabricated boolean.
        """
        from_cache = getattr(self.insights_manager, _INSIGHTS_FROM_CACHE_ATTR, None)
        stale_getter = getattr(self.insights_manager, _INSIGHTS_STALE_ATTR, None)
        stale = stale_getter() if callable(stale_getter) else None
        return {
            "insights_from_cache": None if from_cache is None else bool(from_cache),
            "insights_stale": None if stale is None else bool(stale),
        }

    def _pending_light_actions(self) -> int | None:
        """
        Return the number of light actions awaiting replay.

        Neutral path (ticket 04 not merged): None, meaning "unknown".
        """
        counter = getattr(self.light_learning, _PENDING_COUNT_ATTR, None)
        if not callable(counter):
            return None
        try:
            return int(counter())
        except Exception:  # pragma: no cover - defensive, counter is read-only
            return None

    # -- Recovery sequence -----------------------------------------------------

    def _area_ids(self) -> list[str]:
        """Return the list of area IDs, or an empty list if unavailable."""
        try:
            from homeassistant.helpers import area_registry as ar

            return [area.id for area in ar.async_get(self.hass).async_list_areas()]
        except Exception as err:
            _LOGGER.debug(f"Could not list areas for cloud recovery: {err}")
            return []

    async def _resolve_identity(self) -> bool:
        """Resolve and persist the instance identity if still unknown."""
        instance_id = await self.coordinator.get_or_create_instance_id()
        return bool(instance_id)

    async def _sync_apps(self) -> bool:
        """Re-sync apps and activities from the cloud."""
        instance_id = await self.coordinator.get_or_create_instance_id()
        return bool(
            await self.coordinator.app_storage.async_sync_from_cloud(
                self.coordinator.supabase_client, instance_id, self._area_ids()
            )
        )

    async def _reload_insights(self) -> bool:
        """Reload insights from the cloud, resetting their provenance."""
        instance_id = await self.coordinator.get_or_create_instance_id()
        return bool(await self.insights_manager.async_reload(instance_id))

    async def _replay_light_actions(self) -> bool:
        """
        Replay the buffered light actions through ticket 04's replay method.

        Neutral path (ticket 04 not merged): the step is skipped and reported as
        successful. Never implement a replay queue here.
        """
        replay = getattr(self.light_learning, _REPLAY_METHOD_ATTR, None)
        if not callable(replay):
            _LOGGER.debug("No light-action replay method available, skipping step")
            return True
        result = replay()
        if asyncio.iscoroutine(result):
            result = await result
        return result is not False

    async def _async_run_sequence(self) -> bool:
        """
        Run the ordered recovery sequence.

        Returns:
            True if every step succeeded, False otherwise. The availability
            latch is only armed on a fully successful pass, so a partial failure
            is automatically retried by the next probe.
        """
        for name, step in (
            ("identity", self._resolve_identity),
            ("apps", self._sync_apps),
            ("insights", self._reload_insights),
            ("replay", self._replay_light_actions),
        ):
            try:
                if not await step():
                    self._last_failed_step = name
                    _LOGGER.warning(f"Cloud recovery stopped at step {name}")
                    return False
            except Exception as err:
                self._last_failed_step = name
                _LOGGER.warning(f"Cloud recovery aborted at step {name}: {err}")
                return False

        self._last_failed_step = None
        self._was_available = True
        # A pass only succeeds by talking to the cloud: keep the snapshot
        # consistent for a manual recovery, which does not go through the probe.
        self._available = True
        self._last_success = datetime.now(UTC).isoformat()
        _LOGGER.info("Cloud recovery complete, nominal mode restored")
        return True

    async def async_run_recovery(self) -> bool:
        """
        Run a recovery pass, waiting for any pass already in flight.

        This is the manual entry point (`sync_now` service): an explicit trigger
        must never be silently ignored.
        """
        async with self._lock:
            result = await self._async_run_sequence()
        self._notify_listeners()
        return result

    async def async_probe(self, _now: Any = None) -> None:
        """
        Probe cloud availability and recover on the unavailable -> available edge.

        Scheduled by `async_track_time_interval`. Only the edges are logged: the
        Supabase client owns per-call failure logging.
        """
        try:
            available = await self._is_available()
        except Exception as err:
            # Never let a probe failure escape the timer callback: that would
            # freeze the snapshot on its last value forever. Treat it as
            # unavailable so the latch disarms and a real return triggers a pass.
            _LOGGER.debug(f"Cloud availability probe failed: {err}")
            available = False
        self._available = available

        if not available:
            if self._was_available:
                _LOGGER.warning("Cloud unavailable, entering degraded mode")
            self._was_available = False
            self._notify_listeners()
            return

        if self._was_available:
            return

        if self._lock.locked():
            # A pass is already running: drop this tick instead of queueing it.
            return

        _LOGGER.info("Cloud available again, starting automatic recovery")
        await self.async_run_recovery()

    def _notify_listeners(self) -> None:
        """Push the refreshed status to the entities, best effort."""
        try:
            self.coordinator.async_update_listeners()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug(f"Could not notify listeners after cloud recovery: {err}")

    # -- Observability ---------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """
        Return a synchronous, I/O-free snapshot of the cloud state.

        Consumed by `sensor.linus_brain_cloud_health`. Every value is machine
        data (bool, int, ISO 8601 string or None), never a human sentence.
        """
        status = {
            "is_available": self._available,
            "is_degraded": not self._available,
            "pending_light_actions": self._pending_light_actions(),
            "last_recovery_success": self._last_success,
            "last_failed_step": self._last_failed_step,
        }
        status.update(self._insights_provenance())
        return status
