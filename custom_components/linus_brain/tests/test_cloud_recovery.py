"""
Tests for the cloud recovery manager and the cloud health sensor.

Covers the automatic return to nominal mode (ordered recovery sequence, single
pass per edge, partial-failure retry) and the real observability of degraded
mode through `sensor.linus_brain_cloud_health`.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers.event import async_track_time_interval
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from ..const import RECOVERY_PROBE_INTERVAL
from ..sensor import LinusBrainCloudHealthSensor
from ..utils import supabase_client as supabase_client_module
from ..utils.cloud_recovery import CloudRecoveryManager
from ..utils.supabase_client import (
    CIRCUIT_COOLDOWN_SECONDS,
    FAILURE_THRESHOLD,
    SupabaseClient,
)


class FakeSupabaseClient:
    """Supabase client double exposing `is_available()` and `async_ping()`."""

    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available

    async def async_ping(self) -> bool:
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


async def test_probe_awaits_async_ping():
    """The probe awaits `async_ping()` on every tick, nominal or degraded."""
    manager, calls, parts = build_manager()
    coordinator = parts[0]

    state = {"available": False, "pings": 0}

    async def async_ping() -> bool:
        state["pings"] += 1
        return state["available"]

    coordinator.supabase_client.async_ping = async_ping

    await manager.async_probe()
    assert manager._was_available is False

    state["available"] = True
    await manager.async_probe()
    await manager.async_probe()

    assert state["pings"] == 3
    assert calls == ["identity", "apps", "insights", "replay"]


# --- Transitional joints (tickets 03/04 not merged) -------------------------


async def test_neutral_joint_never_triggers_a_pass():
    """Without ticket 03/04 symbols, provenance is unknown and nothing runs."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
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


async def test_raising_probe_never_escapes_the_timer_callback():
    """A probe that raises is treated as unavailable, never propagated."""
    calls: list = []
    coordinator = FakeCoordinator(calls)
    insights = FakeInsightsManager(calls)
    light_learning = MagicMock(spec=[])
    manager = CloudRecoveryManager(MagicMock(), coordinator, insights, light_learning)

    async def boom() -> bool:
        raise RuntimeError("probe exploded")

    coordinator.supabase_client.async_ping = boom

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


# --- Real Supabase client: the probe drives real I/O ------------------------

RECOVERY_LOGGER = "custom_components.linus_brain.utils.cloud_recovery"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _Response:
    """Minimal aiohttp response double usable as an async context manager."""

    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class JournalSession:
    """
    Fake aiohttp session logging every call (verb, path, mode).

    `mode` is one of "ok", "timeout", "http_503". When `advance` is set, the
    clock moves forward by that many seconds before the call answers or raises,
    like a real request that takes time.
    """

    def __init__(self, freezer) -> None:
        self._freezer = freezer
        self.mode = "ok"
        self.advance = 0.0
        self.journal: list[tuple[str, str, str]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        self.journal.append(("GET", path, self.mode))
        if self.advance:
            self._freezer.tick(timedelta(seconds=self.advance))
        if self.mode == "timeout":
            raise TimeoutError()
        if self.mode == "http_503":
            return _Response(503, text="unavailable")
        return _Response(200, payload=[])


class TestRealClientProbe:
    """The probe runs a real SupabaseClient against a journaling fake session."""

    @pytest.fixture
    def env(self, hass, freezer):
        """Wire a real client, a real manager and the real probe timer."""
        freezer.move_to(T0)
        session = JournalSession(freezer)
        with patch.object(
            supabase_client_module,
            "async_get_clientsession",
            return_value=session,
        ):
            client = SupabaseClient(MagicMock(), "https://demo.supabase.co", "key")

        calls: list = []
        coordinator = FakeCoordinator(calls)
        coordinator.supabase_client = client
        insights = FakeInsightsManager(calls)
        light_learning = FakeLightLearning(calls)
        manager = CloudRecoveryManager(
            MagicMock(), coordinator, insights, light_learning
        )
        coordinator.cloud_recovery = manager

        unsub = async_track_time_interval(
            hass, manager.async_probe, timedelta(seconds=RECOVERY_PROBE_INTERVAL)
        )

        async def tick(count: int) -> None:
            """Fire the probe timer at its next `count` nominal instants."""
            for _ in range(count):
                tick.index += 1
                freezer.move_to(
                    T0 + timedelta(seconds=tick.index * RECOVERY_PROBE_INTERVAL)
                )
                async_fire_time_changed(hass)
                await hass.async_block_till_done()

        tick.index = 0

        yield {
            "session": session,
            "client": client,
            "manager": manager,
            "coordinator": coordinator,
            "calls": calls,
            "tick": tick,
            "freezer": freezer,
        }
        unsub()

    async def test_nominal_tick_is_one_call_and_no_recovery_pass(self, env):
        """(a) One light GET per tick in nominal mode, never a recovery pass."""
        await env["tick"](1)

        assert env["session"].journal == [("GET", "/rest/v1/ha_instances", "ok")]
        assert env["calls"] == []
        assert env["manager"].get_status()["is_available"] is True

    @pytest.mark.parametrize("mode", ["timeout", "http_503"])
    async def test_unreachable_cloud_is_detected_and_opens_the_circuit(self, env, mode):
        """(b) Without any other traffic, 3 failing ticks open the circuit."""
        env["session"].mode = mode

        await env["tick"](FAILURE_THRESHOLD)

        assert len(env["session"].journal) == FAILURE_THRESHOLD
        assert env["client"].is_available() is False
        assert env["client"]._circuit_is_open() is True
        status = env["manager"].get_status()
        assert status["is_available"] is False
        assert status["is_degraded"] is True

    async def test_tick_inside_the_circuit_window_sends_nothing(self, env):
        """(c) Circuit opened off-tick at tick + 30 s: the next tick is skipped."""
        client, session, freezer = env["client"], env["session"], env["freezer"]
        await env["tick"](1)
        assert len(session.journal) == 1

        # 3 failures caused by other traffic, 30 s after the tick: the circuit
        # stays open until tick + 90 s, so tick + 60 s falls inside the window.
        session.mode = "timeout"
        freezer.move_to(T0 + timedelta(seconds=RECOVERY_PROBE_INTERVAL + 30))
        for _ in range(FAILURE_THRESHOLD):
            assert await client.fetch_rules() is None
        entries = len(session.journal)
        assert client._circuit_is_open() is True

        await env["tick"](1)

        assert len(session.journal) == entries
        assert env["manager"].get_status()["is_available"] is False

        # The following tick is past the window: the probe goes out again.
        await env["tick"](1)
        assert len(session.journal) == entries + 1

    async def test_probe_duration_skips_one_tick_then_resumes(self, env):
        """(d) A probe taking 1 s pushes the window past the next tick once."""
        session = env["session"]
        session.mode = "timeout"
        session.advance = 1

        await env["tick"](FAILURE_THRESHOLD)
        assert len(session.journal) == FAILURE_THRESHOLD
        assert env["client"]._circuit_is_open() is True

        # The circuit opened 1 s after the 3rd tick and lasts one cooldown: the
        # 4th tick (exactly one interval after the 3rd) is still inside it.
        assert CIRCUIT_COOLDOWN_SECONDS == RECOVERY_PROBE_INTERVAL
        await env["tick"](1)
        assert len(session.journal) == FAILURE_THRESHOLD

        # The 5th tick is past the window: the probe goes out (and fails again).
        await env["tick"](1)
        assert len(session.journal) == FAILURE_THRESHOLD + 1

    @pytest.mark.parametrize("mode", ["timeout", "http_503"])
    async def test_recovery_within_two_ticks_after_the_cloud_returns(
        self, env, caplog, mode
    ):
        """(e) + (f) Nominal mode is restored in <= 2 ticks, one pass, no ERROR."""
        caplog.set_level(logging.DEBUG, logger=RECOVERY_LOGGER)
        session, manager = env["session"], env["manager"]
        session.mode = mode
        await env["tick"](FAILURE_THRESHOLD)
        assert manager.get_status()["is_available"] is False
        assert env["calls"] == []
        journal_when_back = len(session.journal)

        session.mode = "ok"
        await env["tick"](2)

        first_success = session.journal[journal_when_back]
        assert first_success == ("GET", "/rest/v1/ha_instances", "ok")
        assert manager.get_status()["is_available"] is True
        assert env["calls"] == ["identity", "apps", "insights", "replay"]
        assert env["coordinator"].app_storage.sync_calls == 1

        sensor = LinusBrainCloudHealthSensor(
            build_sensor_coordinator_for(manager), _entry()
        )
        assert sensor._attr_native_value == "connected"

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors == []

    async def test_sensor_is_disconnected_while_the_cloud_is_down(self, env):
        """The health sensor follows the real manager into degraded mode."""
        env["session"].mode = "http_503"
        await env["tick"](1)

        sensor = LinusBrainCloudHealthSensor(
            build_sensor_coordinator_for(env["manager"]), _entry()
        )

        assert sensor._attr_native_value == "disconnected"
        assert sensor._attr_extra_state_attributes["is_degraded"] is True


def _entry():
    """Config entry double for the sensor."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def build_sensor_coordinator_for(manager):
    """Sensor coordinator double carrying the real manager."""
    coordinator = build_sensor_coordinator(None)
    coordinator.cloud_recovery = manager
    return coordinator
