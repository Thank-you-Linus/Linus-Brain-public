"""
Tests for the cloud recovery manager and the cloud health sensor.

Covers the automatic return to nominal mode (ordered recovery sequence, single
pass per edge, partial-failure retry) and the real observability of degraded
mode through `sensor.linus_brain_cloud_health`.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from ..const import RECOVERY_PROBE_INTERVAL
from ..sensor import LinusBrainCloudHealthSensor
from ..utils.cloud_recovery import CloudRecoveryManager


class FakeSupabaseClient:
    """Supabase client double exposing ticket 02's `is_available()`."""

    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available


class FakeAppStorage:
    """App storage double recording cloud syncs."""

    def __init__(self, calls: list, result: bool = True) -> None:
        self._calls = calls
        self.result = result
        self.sync_calls = 0

    async def async_sync_from_cloud(self, client, instance_id, area_ids) -> bool:
        self._calls.append("apps")
        self.sync_calls += 1
        return self.result

    def get_apps(self) -> list:
        return []

    def get_activities(self) -> list:
        return []


class FakeCoordinator:
    """Coordinator double carrying only what the manager and sensor read."""

    def __init__(self, calls: list, available: bool = True) -> None:
        self._calls = calls
        self.supabase_client = FakeSupabaseClient(available)
        self.app_storage = FakeAppStorage(calls)
        self.supabase_url = "https://test.supabase.co"
        self.instance_id = "instance-1"
        self.last_sync_time = None
        self.sync_count = 1
        self.error_count = 0
        self.listener_updates = 0

    async def get_or_create_instance_id(self) -> str:
        if "identity" not in self._calls:
            self._calls.append("identity")
        return "instance-1"

    def async_update_listeners(self) -> None:
        self.listener_updates += 1


class FakeInsightsManager:
    """Insights manager double exposing ticket 03's provenance symbols."""

    def __init__(self, calls: list, result: bool = True) -> None:
        self._calls = calls
        self.result = result
        self._stale = True
        self.loaded_from_cache = True
        self.reload_calls = 0

    def is_stale(self) -> bool:
        return self._stale

    async def async_reload(self, instance_id: str) -> bool:
        self._calls.append("insights")
        self.reload_calls += 1
        if self.result:
            self._stale = False
            self.loaded_from_cache = False
        return self.result


class FakeLightLearning:
    """Light learning double exposing ticket 04's replay symbols."""

    def __init__(self, calls: list, pending: int = 2, result: bool = True) -> None:
        self._calls = calls
        self._pending = pending
        self.result = result
        self.replay_calls = 0

    def get_pending_count(self) -> int:
        return self._pending

    async def async_replay_pending_actions(self) -> bool:
        self._calls.append("replay")
        self.replay_calls += 1
        if self.result:
            self._pending = 0
        return self.result


def build_manager(
    available: bool = True,
    apps_result: bool = True,
    insights_result: bool = True,
    replay_result: bool = True,
    pending: int = 2,
):
    """Build a manager wired to fakes, returning (manager, calls, parts)."""
    calls: list = []
    hass = MagicMock()
    hass.data = {}
    coordinator = FakeCoordinator(calls, available=available)
    coordinator.app_storage.result = apps_result
    insights = FakeInsightsManager(calls, result=insights_result)
    light_learning = FakeLightLearning(calls, pending=pending, result=replay_result)
    manager = CloudRecoveryManager(hass, coordinator, insights, light_learning)
    return manager, calls, (coordinator, insights, light_learning)


# --- Initial state ----------------------------------------------------------


def test_starts_in_nominal_mode():
    """The availability latch starts armed: setup has just done a full load."""
    manager, _calls, _parts = build_manager()

    assert manager._was_available is True
    assert manager._available is True
    assert manager.get_status()["is_degraded"] is False


async def test_probe_while_available_never_runs_a_pass():
    """A probe on an already-available cloud is a no-op."""
    manager, calls, _parts = build_manager(available=True)

    await manager.async_probe()

    assert calls == []


# --- AC 1: one ordered recovery pass per edge -------------------------------


async def test_transition_runs_one_ordered_recovery_pass():
    """Unavailable -> available fires exactly one pass, in the required order."""
    manager, calls, parts = build_manager(available=True)
    coordinator, insights, light_learning = parts

    # Go down first: this disarms the latch.
    coordinator.supabase_client.available = False
    await manager.async_probe()
    assert calls == []
    assert manager._was_available is False

    # Cloud comes back: one automatic pass, no manual service call.
    coordinator.supabase_client.available = True
    await manager.async_probe()

    assert calls == ["identity", "apps", "insights", "replay"]
    assert insights.reload_calls == 1
    assert light_learning.replay_calls == 1
    assert manager._was_available is True


async def test_recovery_runs_only_once_per_edge():
    """Further probes while available do not replay the sequence."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]

    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True

    for _ in range(3):
        await manager.async_probe()

    assert calls == ["identity", "apps", "insights", "replay"]
    assert coordinator.app_storage.sync_calls == 1


async def test_failed_step_skips_the_rest_and_retries_next_probe():
    """A failing step stops the pass and leaves the latch disarmed."""
    manager, calls, parts = build_manager(apps_result=False)
    coordinator, insights, light_learning = parts

    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True
    await manager.async_probe()

    assert calls == ["identity", "apps"]
    assert insights.reload_calls == 0
    assert light_learning.replay_calls == 0
    assert manager._was_available is False
    assert manager.get_status()["last_failed_step"] == "apps"

    # Next probe replays the whole sequence from the beginning.
    coordinator.app_storage.result = True
    calls.clear()
    await manager.async_probe()

    assert calls == ["identity", "apps", "insights", "replay"]
    assert manager._was_available is True
    assert manager.get_status()["last_failed_step"] is None


async def test_step_exception_never_escapes_the_probe():
    """An exception inside a step aborts the pass without propagating."""
    manager, _calls, parts = build_manager()
    coordinator = parts[0]

    async def boom(*args, **kwargs):
        raise RuntimeError("cloud exploded")

    coordinator.app_storage.async_sync_from_cloud = boom
    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True

    await manager.async_probe()

    assert manager._was_available is False
    assert manager.get_status()["last_failed_step"] == "apps"


async def test_probe_tick_during_a_pass_is_dropped():
    """A tick landing during a running pass is dropped, not queued."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]
    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sync(*args, **kwargs):
        calls.append("apps")
        started.set()
        await release.wait()
        return True

    coordinator.app_storage.async_sync_from_cloud = slow_sync

    task = asyncio.create_task(manager.async_probe())
    await started.wait()

    await manager.async_probe()  # dropped, lock is held

    release.set()
    await task

    assert calls.count("apps") == 1


async def test_manual_recovery_waits_for_the_running_pass():
    """A manual trigger waits for the lock instead of being ignored."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sync(*args, **kwargs):
        calls.append("apps")
        started.set()
        await release.wait()
        return True

    coordinator.app_storage.async_sync_from_cloud = slow_sync

    task = asyncio.create_task(manager.async_run_recovery())
    await started.wait()

    manual = asyncio.create_task(manager.async_run_recovery())
    await asyncio.sleep(0)
    assert not manual.done()

    release.set()
    await task
    assert await manual is True
    assert calls.count("apps") == 2


# --- AC 2: state after recovery ---------------------------------------------


async def test_after_recovery_insights_are_fresh_and_buffer_is_empty():
    """Recovery resets insights provenance and drains the light-action buffer."""
    manager, _calls, parts = build_manager(pending=3)
    coordinator, insights, light_learning = parts

    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True
    await manager.async_probe()

    assert insights.is_stale() is False
    assert insights.loaded_from_cache is False
    assert light_learning.get_pending_count() == 0

    status = manager.get_status()
    assert status["insights_stale"] is False
    assert status["insights_from_cache"] is False
    assert status["pending_light_actions"] == 0
    assert status["is_degraded"] is False
    assert status["last_recovery_success"] is not None


async def test_async_is_available_is_awaited():
    """An async `is_available()` (ticket 02) is awaited, not truth-tested."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]

    state = {"available": False}

    async def is_available() -> bool:
        return state["available"]

    coordinator.supabase_client.is_available = is_available

    await manager.async_probe()
    assert manager._was_available is False

    state["available"] = True
    await manager.async_probe()

    assert calls == ["identity", "apps", "insights", "replay"]


# --- Transitional joints (tickets 02/03/04 not merged) ----------------------


async def test_neutral_joint_never_triggers_a_pass():
    """Without ticket 02's symbol, availability is assumed and nothing runs."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
    coordinator.supabase_client = MagicMock(spec=[])
    insights = MagicMock(spec=[])
    light_learning = MagicMock(spec=[])
    manager = CloudRecoveryManager(MagicMock(), coordinator, insights, light_learning)

    await manager.async_probe()

    assert calls == []
    status = manager.get_status()
    assert status["is_degraded"] is False
    assert status["pending_light_actions"] is None
    assert status["insights_from_cache"] is None
    assert status["insights_stale"] is None


async def test_missing_replay_method_is_a_skipped_step():
    """Without ticket 04's replay method the step is skipped, not failed."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
    insights = FakeInsightsManager(calls)
    light_learning = MagicMock(spec=[])
    manager = CloudRecoveryManager(MagicMock(), coordinator, insights, light_learning)

    coordinator.supabase_client.available = False
    await manager.async_probe()
    coordinator.supabase_client.available = True
    await manager.async_probe()

    assert calls == ["identity", "apps", "insights"]
    assert manager._was_available is True


async def test_non_callable_availability_attribute_is_read_not_called():
    """Ticket 02 may expose availability as a property: read it, never call it."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
    insights = FakeInsightsManager(calls)
    light_learning = MagicMock(spec=[])
    manager = CloudRecoveryManager(MagicMock(), coordinator, insights, light_learning)

    # A plain bool attribute must not raise "'bool' object is not callable".
    coordinator.supabase_client.is_available = False
    await manager.async_probe()
    assert manager._was_available is False
    assert calls == []

    coordinator.supabase_client.is_available = True
    await manager.async_probe()

    assert calls == ["identity", "apps", "insights"]


async def test_raising_probe_never_escapes_the_timer_callback():
    """A probe that raises is treated as unavailable, never propagated."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
    insights = FakeInsightsManager(calls)
    light_learning = MagicMock(spec=[])
    manager = CloudRecoveryManager(MagicMock(), coordinator, insights, light_learning)

    def boom() -> bool:
        raise RuntimeError("probe exploded")

    coordinator.supabase_client.is_available = boom

    await manager.async_probe()

    assert manager.get_status()["is_available"] is False
    assert manager._was_available is False
    assert calls == []


async def test_manual_recovery_clears_the_degraded_snapshot():
    """A successful manual pass restores availability, not just the latch."""
    manager, _calls, parts = build_manager()
    coordinator = parts[0]

    coordinator.supabase_client.available = False
    await manager.async_probe()
    assert manager.get_status()["is_degraded"] is True

    # `sync_now` recovers without going through the probe.
    coordinator.supabase_client.available = True
    assert await manager.async_run_recovery() is True

    status = manager.get_status()
    assert status["is_available"] is True
    assert status["is_degraded"] is False


# --- Probe wiring -----------------------------------------------------------


def test_probe_interval_constant():
    """The probe cadence is short enough to feel automatic."""
    assert RECOVERY_PROBE_INTERVAL == 60


async def test_setup_callback_delegates_to_the_manager():
    """The `async_refresh_remote_config` callback is a thin delegation."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]
    coordinator.cloud_recovery = manager

    async def async_refresh_remote_config(_now=None):
        await coordinator.cloud_recovery.async_probe()

    coordinator.supabase_client.available = False
    await async_refresh_remote_config(None)
    coordinator.supabase_client.available = True
    await async_refresh_remote_config(None)

    assert calls == ["identity", "apps", "insights", "replay"]


# --- AC 3 / AC 4: cloud health sensor ---------------------------------------


@pytest.fixture
def sensor_entry():
    """Config entry double for the sensor."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def build_sensor_coordinator(status=None):
    """Coordinator double for the sensor, optionally carrying a status."""
    coordinator = MagicMock()
    coordinator.error_count = 0
    coordinator.sync_count = 1
    coordinator.last_sync_time = "2026-01-01T00:00:00+00:00"
    coordinator.instance_id = "instance-1"
    coordinator.supabase_url = "https://test.supabase.co"
    coordinator.app_storage.get_apps.return_value = []
    coordinator.app_storage.get_activities.return_value = []
    if status is None:
        coordinator.cloud_recovery = None
    else:
        coordinator.cloud_recovery = MagicMock()
        coordinator.cloud_recovery.get_status.return_value = status
    return coordinator


def test_sensor_disconnected_when_cloud_unavailable(sensor_entry):
    """AC 3: the sensor leaves `connected` as soon as the cloud is down."""
    status = {
        "is_available": False,
        "is_degraded": True,
        "pending_light_actions": 2,
        "last_recovery_success": None,
        "last_failed_step": None,
        "insights_from_cache": True,
        "insights_stale": True,
    }
    sensor = LinusBrainCloudHealthSensor(build_sensor_coordinator(status), sensor_entry)

    assert sensor._attr_native_value == "disconnected"
    assert sensor._attr_native_value != "connected"
    assert sensor._attr_icon == "mdi:cloud-off-outline"
    assert sensor._attr_options == ["connected", "disconnected", "error"]

    attributes = sensor._attr_extra_state_attributes
    assert attributes["is_degraded"] is True
    assert attributes["pending_light_actions"] == 2
    assert attributes["insights_from_cache"] is True
    assert attributes["insights_stale"] is True
    assert "error" not in attributes


def test_sensor_connected_after_recovery(sensor_entry):
    """AC 3: the sensor returns to `connected` once recovery has run."""
    status = {
        "is_available": True,
        "is_degraded": False,
        "pending_light_actions": 0,
        "last_recovery_success": "2026-01-01T10:00:00+00:00",
        "last_failed_step": None,
        "insights_from_cache": False,
        "insights_stale": False,
    }
    sensor = LinusBrainCloudHealthSensor(build_sensor_coordinator(status), sensor_entry)

    assert sensor._attr_native_value == "connected"
    assert sensor._attr_icon == "mdi:cloud-check"

    attributes = sensor._attr_extra_state_attributes
    assert attributes["is_degraded"] is False
    assert attributes["pending_light_actions"] == 0
    assert attributes["insights_stale"] is False
    assert attributes["last_successful_sync"] == "2026-01-01T10:00:00+00:00"


def test_sensor_error_when_recovery_incomplete(sensor_entry):
    """Cloud reachable but the last pass failed: `error`, the third enum value."""
    status = {
        "is_available": True,
        "is_degraded": False,
        "pending_light_actions": 4,
        "last_recovery_success": None,
        "last_failed_step": "apps",
        "insights_from_cache": True,
        "insights_stale": True,
    }
    sensor = LinusBrainCloudHealthSensor(build_sensor_coordinator(status), sensor_entry)

    assert sensor._attr_native_value == "error"
    assert sensor._attr_icon == "mdi:cloud-alert"


def test_sensor_falls_back_to_counters_without_manager(sensor_entry):
    """AC 4: with no manager attached, the legacy heuristic still applies."""
    coordinator = build_sensor_coordinator(None)
    sensor = LinusBrainCloudHealthSensor(coordinator, sensor_entry)

    assert sensor._attr_native_value == "connected"

    attributes = sensor._attr_extra_state_attributes
    # Legacy attributes are untouched...
    assert attributes["status"] == "connected"
    assert attributes["total_syncs"] == 1
    assert attributes["total_errors"] == 0
    assert attributes["instance_id"] == "instance-1"
    assert attributes["supabase_url"] == "https://test.supabase.co"
    assert attributes["last_successful_sync"] == "2026-01-01T00:00:00+00:00"
    # ...and the new keys are present with neutral values.
    assert attributes["is_degraded"] is False
    assert attributes["pending_light_actions"] is None
    assert attributes["insights_from_cache"] is None
    assert attributes["insights_stale"] is None


def test_sensor_legacy_disconnected_without_manager(sensor_entry):
    """Without a manager, a zero sync count still reads as `disconnected`."""
    coordinator = build_sensor_coordinator(None)
    coordinator.sync_count = 0
    sensor = LinusBrainCloudHealthSensor(coordinator, sensor_entry)

    assert sensor._attr_native_value == "disconnected"
    assert sensor._attr_extra_state_attributes["is_degraded"] is True


def test_sensor_state_stays_in_declared_options(sensor_entry):
    """Every computed state must remain one of the three declared options."""
    for status in (
        {"is_available": False, "is_degraded": True},
        {"is_available": True, "is_degraded": False, "last_failed_step": "insights"},
        {"is_available": True, "is_degraded": False, "pending_light_actions": 0},
    ):
        sensor = LinusBrainCloudHealthSensor(
            build_sensor_coordinator(status), sensor_entry
        )
        assert sensor._attr_native_value in sensor._attr_options
