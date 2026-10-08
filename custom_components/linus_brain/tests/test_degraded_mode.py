"""
End-to-end tests of the degraded mode (Supabase unreachable) and of the recovery.

Complements test_offline_setup.py, which covers the identity persistence of the
degraded setup path with a mocked Supabase client. This module goes through the
real SupabaseClient (availability state, circuit breaker), the real config entry
setup and the real CloudRecoveryManager timer: only the HTTP session is faked.

Key responsibilities:
- Cold start with the database down (scenario 1): setup completes, button /
  sensor / switch entities are really mounted, activity tracking and rule
  evaluation run on the local cache or const.py
- Database falling during operation (scenario 2): insights already loaded keep
  being served, a manual light action is buffered and persisted, nothing raises
- Database coming back (scenario 3): the periodic probe alone, driven by the
  advance of time, restores the insights, drains the light-action buffer and
  brings sensor.linus_brain_cloud_health back to nominal
- Each scenario runs against both failure modes: a timeout and an HTTP 503
- Log guard: no ERROR record from the integration outside the documented
  exemptions
- Scope contract: every bullet of "Ce qui continue de fonctionner" in
  docs/DEGRADED_MODE.md is mapped, by DEGRADED_SCOPE below, to the test that
  locks it, and a meta-test fails when the table and the document diverge
"""

import importlib
import logging
import re
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from ..const import (
    CONF_SUPABASE_KEY,
    CONF_SUPABASE_URL,
    DOMAIN,
    RECOVERY_PROBE_INTERVAL,
)

# ---------------------------------------------------------------------------
# Scope contract: bullet of docs/DEGRADED_MODE.md -> test that locks it
# ---------------------------------------------------------------------------
# Keys are the bold titles of the bullets of the section
# "## ✅ Ce qui continue de fonctionner". Values are "Class::test" names of this
# module. TestDegradedScopeContract fails when a bullet has no test, when a
# named test does not exist, or when the table and the document do not count
# the same bullets.
DEGRADED_SCOPE: dict[str, str] = {
    "Suivi d'activité": "TestColdStartCloudDown::test_activity_tracking_follows_motion",
    "Évaluation des règles": (
        "TestColdStartCloudDown::test_rule_evaluation_runs_on_local_or_const_app"
    ),
    "Éclairage automatique": (
        "TestColdStartCloudDown::test_automatic_lighting_calls_the_light_service"
    ),
    "Feature flags par zone": (
        "TestColdStartCloudDown::test_disabled_feature_flag_blocks_the_action"
    ),
    "Capteurs de diagnostic": (
        "TestColdStartCloudDown::test_diagnostic_entities_are_mounted_and_report_degraded"
    ),
    "Insights IA": (
        "TestCloudFallsDuringOperation::test_loaded_insights_are_still_served"
    ),
}

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "DEGRADED_MODE.md"
DOC_SECTION_TITLE = "## ✅ Ce qui continue de fonctionner"

FAILURE_MODES = ["timeout", "http_503"]

HA_UUID = "ha-installation-uuid-degraded"
INSTANCE_ID = "cloud-instance-uuid-degraded"
USER_ID = "user-degraded-0001"

AREA_NAME = "Salon"
AREA_ID = "salon"
LIGHT_ID = "light.salon_lamp"
MOTION_ID = "binary_sensor.salon_motion"
LUX_ID = "sensor.salon_lux"
CLOUD_HEALTH_ID = "sensor.linus_brain_cloud_health"
FEATURE_SWITCH_ID = f"switch.linus_brain_feature_automatic_lighting_{AREA_ID}"
SYNC_BUTTON_UNIQUE_ID = "sync"
ACTIVITY_SENSOR_UNIQUE_ID = f"{DOMAIN}_activity_{AREA_ID}"
FEATURE_SWITCH_UNIQUE_ID = f"{DOMAIN}_feature_automatic_lighting_{AREA_ID}"

INSIGHT_TYPE = "dark_threshold_lux"
INSIGHT_BEFORE = 123
INSIGHT_AFTER = 456

# Loggers allowed to emit ERROR records while the database is unreachable: the
# Supabase client, the coordinator and the insights manager report the outage
# itself. Any other module of the package must stay clean.
LOGGER_ROOT = "custom_components.linus_brain"
EXEMPT_ERROR_LOGGERS = (
    "custom_components.linus_brain.utils.supabase_client",
    "custom_components.linus_brain.coordinator",
    "custom_components.linus_brain.utils.insights_manager",
)


# ---------------------------------------------------------------------------
# Fake cloud: a state router standing in for the aiohttp session
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal aiohttp response usable as an async context manager."""

    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self) -> Any:
        """Return the decoded JSON payload."""
        return self._payload

    async def text(self) -> str:
        """Return the raw body."""
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        """Enter the async context."""
        return self

    async def __aexit__(self, *exc: object) -> bool:
        """Leave the async context without swallowing exceptions."""
        return False


class FakeCloud:
    """
    Routing double of the aiohttp session used by the real SupabaseClient.

    `mode` is the state of the database: "ok", "timeout" (every call raises
    TimeoutError) or "http_503" (every call answers 503). Making the database
    fall or come back is one assignment. Every call is journaled with the mode
    that served it, so a test can prove which traffic actually reached the cloud.
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.insight_threshold = INSIGHT_BEFORE
        self.journal: list[dict[str, Any]] = []
        self.received_light_actions: list[dict[str, Any]] = []

    # -- session API used by SupabaseClient ------------------------------------

    def get(self, url: str, params: dict[str, Any] | None = None, **_: Any):
        """Serve a GET."""
        return self._serve("GET", url, params, None)

    def post(
        self,
        url: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        **_: Any,
    ):
        """Serve a POST."""
        return self._serve("POST", url, params, json)

    def patch(
        self,
        url: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        **_: Any,
    ):
        """Serve a PATCH."""
        return self._serve("PATCH", url, params, json)

    # -- routing ---------------------------------------------------------------

    def _serve(
        self, verb: str, url: str, params: dict[str, Any] | None, body: Any
    ) -> _FakeResponse:
        path = urlsplit(url).path.removeprefix("/rest/v1/")
        entry = {
            "verb": verb,
            "path": path,
            "params": dict(params or {}),
            "served": self.mode,
        }
        self.journal.append(entry)

        if self.mode == "timeout":
            raise TimeoutError("fake cloud timeout")
        if self.mode == "http_503":
            return _FakeResponse(503, text="Service Unavailable")

        return self._route(verb, path, body)

    def _route(self, verb: str, path: str, body: Any) -> _FakeResponse:
        if verb == "PATCH":
            return _FakeResponse(204)

        if verb == "POST":
            if path == "light_actions":
                self.received_light_actions.append(body)
                return _FakeResponse(204)
            # rpc/get_latest_app_version: no published version -> const.py
            return _FakeResponse(200, None)

        if path == "ha_instances":
            return _FakeResponse(200, [{"instance_id": INSTANCE_ID}])
        if path == "area_insights":
            return _FakeResponse(200, [self.insight_row()])
        return _FakeResponse(200, [])

    def insight_row(self) -> dict[str, Any]:
        """Return the single global insight the cloud serves."""
        return {
            "id": "insight-1",
            "instance_id": None,
            "area_id": None,
            "insight_type": INSIGHT_TYPE,
            "value": {"threshold": self.insight_threshold},
            "confidence": 0.9,
            "metadata": {},
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    # -- journal helpers -------------------------------------------------------

    def count(self, served: str) -> int:
        """Number of calls served in the given mode."""
        return sum(1 for e in self.journal if e["served"] == served)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _integration_module(name: str) -> Any:
    """
    Import a module of the integration the way Home Assistant's loader does.

    A real config entry setup runs the code loaded as
    `custom_components.linus_brain.*`, a different module object from the
    `linus_brain.*` one the relative imports of this test package give. Patches
    must target the former or they miss the code under test.
    """
    return importlib.import_module(f"custom_components.linus_brain.{name}")


def _unexpected_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """
    Return the ERROR records of the integration that are not exempted.

    Scoped on record.name, never on caplog.text: an ERROR emitted by Home
    Assistant itself or by a third party library is not this integration's.
    """
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and (record.name == LOGGER_ROOT or record.name.startswith(f"{LOGGER_ROOT}."))
        and not any(
            record.name == exempt or record.name.startswith(f"{exempt}.")
            for exempt in EXEMPT_ERROR_LOGGERS
        )
    ]


async def _advance(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    """
    Advance the frozen clock, fire the due timers and let the tasks settle.

    Background tasks are awaited too: the executor jobs a recovery pass starts
    (AppStorage writes its cache file) are not tracked as foreground tasks, and
    ticking again before they end would make asyncio.timeout expire the pass.
    """
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)


async def _advance_until(
    hass: HomeAssistant,
    freezer: Any,
    predicate: Callable[[], bool],
    max_ticks: int = 5,
) -> int:
    """
    Advance by RECOVERY_PROBE_INTERVAL ticks until predicate() holds.

    Returns:
        The number of ticks it took. Fails the test if max_ticks is exhausted.
    """
    for tick in range(1, max_ticks + 1):
        await _advance(hass, freezer, RECOVERY_PROBE_INTERVAL)
        if predicate():
            return tick
    pytest.fail(f"condition still false after {max_ticks} probe ticks")


def _doc_bullets(text: str) -> list[str]:
    """Extract the bold bullet titles of the "Ce qui continue de fonctionner" section."""
    section = text.split(DOC_SECTION_TITLE, 1)[1].split("\n---", 1)[0]
    return re.findall(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE)


def _scope_problems(
    bullets: list[str], scope: dict[str, str], namespace: Any
) -> list[str]:
    """
    Compare the document bullets with the scope table.

    Returns:
        One message per problem: a bullet without test, a test named in the
        table that does not exist, or a different number of bullets.
    """
    problems: list[str] = []
    for bullet in bullets:
        if bullet not in scope:
            problems.append(f"bullet without test: {bullet}")
    for bullet, target in scope.items():
        class_name, _, test_name = target.partition("::")
        test_class = getattr(namespace, class_name, None)
        if test_class is None or not callable(getattr(test_class, test_name, None)):
            problems.append(f"test named in the table does not exist: {target}")
        if bullet not in bullets:
            problems.append(f"table entry absent from the document: {bullet}")
    if len(bullets) != len(scope):
        problems.append(
            f"document counts {len(bullets)} bullets, table counts {len(scope)}"
        )
    return problems


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_cloud() -> FakeCloud:
    """The database, healthy until a test says otherwise."""
    return FakeCloud()


@pytest.fixture
def assert_no_unexpected_errors(caplog: pytest.LogCaptureFixture):
    """
    Arm the log guard and return the check each test calls as its last step.

    Besides the outage reports of the exempted modules, an ERROR record of any
    other module of the package fails the test. This is also what proves that
    capture_light_action lets no exception through: it swallows them as an ERROR
    on utils.light_learning, which is not exempted.
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_ROOT)

    def _check() -> None:
        offenders = [(r.name, r.getMessage()) for r in _unexpected_errors(caplog)]
        assert not offenders, f"unexpected ERROR records: {offenders}"

    return _check


@pytest.fixture
def degraded_hass(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: Any,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> HomeAssistant:
    """
    A Home Assistant with one area holding a light, a motion and a lux sensor.

    The sun is below the horizon and the room is dark, so the default
    automatic_lighting app turns the light on when movement is detected. The
    rule engine debounce is set to zero (test patch): time is only ever moved
    by `_advance`, never slept.
    """
    # pytest-homeassistant-custom-component ships its own `custom_components`
    # package, which shadows the repository one: extend its path so the loader
    # finds `linus_brain` (same approach as test_config_flow.py)
    import custom_components

    root = str(REPO_ROOT / "custom_components")
    if root not in custom_components.__path__:
        custom_components.__path__.append(root)

    monkeypatch.setattr(_integration_module("utils.rule_engine"), "DEBOUNCE_SECONDS", 0)

    # AppStorage writes a real file under the config dir: keep it private to the
    # test so no cache leaks from one test (or one run) to the next
    hass.config.config_dir = str(tmp_path)
    hass.data["core.uuid"] = HA_UUID

    area = ar.async_get(hass).async_create(AREA_NAME)
    assert area.id == AREA_ID

    entity_reg = er.async_get(hass)
    for domain, unique_id, object_id, device_class in (
        ("light", "lamp", "salon_lamp", None),
        ("binary_sensor", "motion", "salon_motion", "motion"),
        ("sensor", "lux", "salon_lux", "illuminance"),
    ):
        entry = entity_reg.async_get_or_create(
            domain,
            "test",
            unique_id,
            suggested_object_id=object_id,
            original_device_class=device_class,
        )
        entity_reg.async_update_entity(entry.entity_id, area_id=AREA_ID)

    hass.states.async_set(LIGHT_ID, "off")
    hass.states.async_set(MOTION_ID, "off", {"device_class": "motion"})
    hass.states.async_set(
        LUX_ID, "5", {"device_class": "illuminance", "unit_of_measurement": "lx"}
    )
    hass.states.async_set("sun.sun", "below_horizon", {"elevation": -10.0})
    return hass


@pytest.fixture
def config_entry(degraded_hass: HomeAssistant) -> MockConfigEntry:
    """The integration's config entry, registered but not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SUPABASE_URL: "https://example.supabase.co",
            CONF_SUPABASE_KEY: "fake-key",
        },
        options={},
    )
    entry.add_to_hass(degraded_hass)

    # The rule engine looks the feature switch up as
    # switch.linus_brain_feature_automatic_lighting_<area>. On a fresh install
    # the pinned Home Assistant derives another id from the device and entity
    # names and the entity-id migration only renames it at the next setup, so
    # the switch is registered under its settled id, as on an existing install.
    er.async_get(degraded_hass).async_get_or_create(
        "switch",
        DOMAIN,
        FEATURE_SWITCH_UNIQUE_ID,
        suggested_object_id=FEATURE_SWITCH_UNIQUE_ID,
        config_entry=entry,
    )
    return entry


@pytest.fixture
async def setup_entry(
    degraded_hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_cloud: FakeCloud,
):
    """
    Yield a coroutine function that sets the entry up against the fake cloud.

    The real SupabaseClient reads its session at construction, so the fake is
    injected through that seam for the duration of the setup only. The entry is
    unloaded at the end of the test to release timers and listeners.
    """

    async def _setup(mode: str) -> bool:
        fake_cloud.mode = mode
        with patch.object(
            _integration_module("utils.supabase_client"),
            "async_get_clientsession",
            return_value=fake_cloud,
        ):
            result = await degraded_hass.config_entries.async_setup(
                config_entry.entry_id
            )
        await degraded_hass.async_block_till_done()
        return result

    yield _setup

    if config_entry.state is ConfigEntryState.LOADED:
        await degraded_hass.config_entries.async_unload(config_entry.entry_id)
        await degraded_hass.async_block_till_done()


def _entry_data(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Return the integration's runtime objects of a loaded entry."""
    return hass.data[DOMAIN][entry.entry_id]


def _cloud_health(hass: HomeAssistant):
    """Return the state object of sensor.linus_brain_cloud_health."""
    state = hass.states.get(CLOUD_HEALTH_ID)
    assert state is not None, f"{CLOUD_HEALTH_ID} is not mounted"
    return state


async def _turn_feature(hass: HomeAssistant, on: bool) -> None:
    """Turn the automatic_lighting switch of the area on or off."""
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": FEATURE_SWITCH_ID},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _manual_light_action(
    hass: HomeAssistant, light_learning: Any, state: str = "on"
) -> None:
    """
    Make a user change the light and hand the change to the light learning.

    The call goes to LightLearning.capture_light_action directly, as the event
    listener would: the event bus does not reach it today (see
    test_manual_light_change_reaches_the_capture_through_the_event_bus).
    """
    context = Context(user_id=USER_ID)
    old_state = hass.states.get(LIGHT_ID)
    hass.states.async_set(LIGHT_ID, state, context=context)
    new_state = hass.states.get(LIGHT_ID)
    await light_learning.capture_light_action(LIGHT_ID, new_state, old_state, context)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Scenario 1: cold start with the database down
# ---------------------------------------------------------------------------


class TestColdStartCloudDown:
    """Setup and real-time core with the cloud unreachable from the first call."""

    pytestmark = pytest.mark.parametrize("failure_mode", FAILURE_MODES)

    async def test_setup_succeeds_and_platforms_are_really_mounted(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Setup returns True and button, sensor and switch entities exist."""
        result = await setup_entry(failure_mode)

        assert result is True
        assert config_entry.state is ConfigEntryState.LOADED

        # The failure mode under test was really served, and nothing else was
        assert fake_cloud.count(failure_mode) >= 1
        assert fake_cloud.count("ok") == 0
        assert (
            _entry_data(degraded_hass, config_entry)["coordinator"].instance_id is None
        )

        # Inspecting PLATFORMS is not enough: the entities must be in the registry
        registry = er.async_get(degraded_hass)
        domains = {
            entry.domain
            for entry in er.async_entries_for_config_entry(
                registry, config_entry.entry_id
            )
        }
        assert {"button", "sensor", "switch"} <= domains
        for domain, unique_id in (
            ("button", SYNC_BUTTON_UNIQUE_ID),
            ("sensor", f"{DOMAIN}_cloud_health"),
            ("switch", FEATURE_SWITCH_UNIQUE_ID),
        ):
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            assert entity_id is not None, f"{domain} {unique_id} not in the registry"
            assert degraded_hass.states.get(entity_id) is not None

        assert_no_unexpected_errors()

    async def test_activity_tracking_follows_motion(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """A motion sensor turning on moves the area to the movement activity."""
        assert await setup_entry(failure_mode) is True
        tracker = _entry_data(degraded_hass, config_entry)["activity_tracker"]
        assert tracker.get_activity(AREA_ID) == "empty"

        degraded_hass.states.async_set(MOTION_ID, "on", {"device_class": "motion"})
        await degraded_hass.async_block_till_done()

        assert tracker.get_activity(AREA_ID) == "movement"
        assert fake_cloud.count(failure_mode) >= 1
        assert_no_unexpected_errors()

    async def test_rule_evaluation_runs_on_local_or_const_app(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """An activity transition evaluates the automatic_lighting app from const.py."""
        from ..const import DEFAULT_AUTOLIGHT_APP

        assert await setup_entry(failure_mode) is True
        coordinator = _entry_data(degraded_hass, config_entry)["coordinator"]

        # No cloud and no local cache on a fresh install: the app comes from const.py
        app = coordinator.app_storage.get_app("automatic_lighting")
        assert app is not None
        assert set(app["activity_actions"]) == set(
            DEFAULT_AUTOLIGHT_APP["activity_actions"]
        )

        async_mock_service(degraded_hass, "light", "turn_on")
        async_mock_service(degraded_hass, "light", "turn_off")
        await _turn_feature(degraded_hass, True)

        degraded_hass.states.async_set(MOTION_ID, "on", {"device_class": "motion"})
        await degraded_hass.async_block_till_done()

        rule = coordinator.last_rules.get(AREA_ID)
        assert rule is not None
        assert rule["rule_name"] == "automatic_lighting:movement"
        assert rule["conditions_met"] is True
        assert fake_cloud.count(failure_mode) >= 1
        assert_no_unexpected_errors()

    async def test_automatic_lighting_calls_the_light_service(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Movement in a dark room turns the light on, without any cloud."""
        assert await setup_entry(failure_mode) is True
        turn_on = async_mock_service(degraded_hass, "light", "turn_on")
        async_mock_service(degraded_hass, "light", "turn_off")
        await _turn_feature(degraded_hass, True)
        turn_on.clear()

        degraded_hass.states.async_set(MOTION_ID, "on", {"device_class": "motion"})
        await degraded_hass.async_block_till_done()

        assert turn_on, "light.turn_on was never called"
        assert LIGHT_ID in turn_on[0].data["entity_id"]
        assert turn_on[0].data["brightness_pct"] == 100
        assert fake_cloud.count(failure_mode) >= 1
        assert_no_unexpected_errors()

    async def test_disabled_feature_flag_blocks_the_action(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """A switch turned off blocks the action; turning it on is honoured."""
        assert await setup_entry(failure_mode) is True
        tracker = _entry_data(degraded_hass, config_entry)["activity_tracker"]
        turn_on = async_mock_service(degraded_hass, "light", "turn_on")
        async_mock_service(degraded_hass, "light", "turn_off")

        # The flag is local and modifiable while the cloud is down
        await _turn_feature(degraded_hass, False)
        assert degraded_hass.states.get(FEATURE_SWITCH_ID).state == "off"

        degraded_hass.states.async_set(MOTION_ID, "on", {"device_class": "motion"})
        await degraded_hass.async_block_till_done()

        # Activities stay tracked, the automation is what the flag blocks
        assert tracker.get_activity(AREA_ID) == "movement"
        assert not turn_on

        await _turn_feature(degraded_hass, True)
        assert degraded_hass.states.get(FEATURE_SWITCH_ID).state == "on"
        assert turn_on, "enabling the flag did not let the action through"
        assert fake_cloud.count(failure_mode) >= 1
        assert_no_unexpected_errors()

    async def test_diagnostic_entities_are_mounted_and_report_degraded(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Diagnostic entities stay available and cloud_health reports the outage."""
        assert await setup_entry(failure_mode) is True

        # The manager starts armed: the first probe tick is what notices the outage
        await _advance(degraded_hass, freezer, RECOVERY_PROBE_INTERVAL)

        health = _cloud_health(degraded_hass)
        assert health.state == "disconnected"
        assert health.attributes["is_degraded"] is True

        # The other diagnostics are mounted and keep refreshing
        registry = er.async_get(degraded_hass)
        button_id = registry.async_get_entity_id(
            "button", DOMAIN, SYNC_BUTTON_UNIQUE_ID
        )
        for entity_id in (button_id, FEATURE_SWITCH_ID):
            assert degraded_hass.states.get(entity_id).state != "unavailable"
        activity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, ACTIVITY_SENSOR_UNIQUE_ID
        )
        assert activity_id is not None
        before = degraded_hass.states.get(activity_id)
        assert before is not None and before.state != "unavailable"

        degraded_hass.states.async_set(MOTION_ID, "on", {"device_class": "motion"})
        await degraded_hass.async_block_till_done()
        assert degraded_hass.states.get(activity_id).state != before.state

        assert fake_cloud.count(failure_mode) >= 1
        assert_no_unexpected_errors()


# ---------------------------------------------------------------------------
# Scenario 2: the database falls during operation
# ---------------------------------------------------------------------------


class TestCloudFallsDuringOperation:
    """Nominal setup, then the database becomes unreachable."""

    pytestmark = pytest.mark.parametrize("failure_mode", FAILURE_MODES)

    async def _nominal_then_down(
        self, hass, entry, setup_entry, fake_cloud, freezer, failure_mode
    ) -> dict[str, Any]:
        """Set up nominally, drop the database, let the probe notice."""
        assert await setup_entry("ok") is True
        data = _entry_data(hass, entry)
        assert data["coordinator"].instance_id == INSTANCE_ID
        assert fake_cloud.count("ok") >= 1

        fake_cloud.mode = failure_mode
        await _advance(hass, freezer, RECOVERY_PROBE_INTERVAL)
        assert fake_cloud.count(failure_mode) >= 1
        return data

    async def test_loaded_insights_are_still_served(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Insights loaded before the outage keep being served from memory."""
        assert await setup_entry("ok") is True
        insights = _entry_data(degraded_hass, config_entry)["insights_manager"]
        served = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert served is not None
        assert served["value"] == {"threshold": INSIGHT_BEFORE}

        fake_cloud.mode = failure_mode
        await _advance(degraded_hass, freezer, RECOVERY_PROBE_INTERVAL)

        health = _cloud_health(degraded_hass)
        assert health.state == "disconnected"
        assert health.attributes["is_degraded"] is True
        assert fake_cloud.count(failure_mode) >= 1

        still_served = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert still_served is not None
        assert still_served["value"] == {"threshold": INSIGHT_BEFORE}

        # A reload attempt during the outage fails without emptying the cache
        assert await insights.async_reload(INSTANCE_ID) is False
        kept = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert kept is not None
        assert kept["value"] == {"threshold": INSIGHT_BEFORE}
        assert_no_unexpected_errors()

    async def test_manual_light_action_is_buffered_and_persisted(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        hass_storage,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """A manual light action during the outage lands in the persisted buffer."""
        data = await self._nominal_then_down(
            degraded_hass, config_entry, setup_entry, fake_cloud, freezer, failure_mode
        )
        light_learning = data["light_learning"]
        assert light_learning.get_pending_count() == 0

        await _manual_light_action(degraded_hass, light_learning)

        assert light_learning.get_pending_count() >= 1
        assert fake_cloud.received_light_actions == []

        persisted = hass_storage["linus_brain.light_actions"]["data"]["entries"]
        assert len(persisted) >= 1
        assert persisted[0]["payload"]["entity_id"] == LIGHT_ID
        assert persisted[0]["payload"]["instance_id"] == INSTANCE_ID
        assert_no_unexpected_errors()

    async def test_capture_light_action_lets_no_exception_through(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Capturing several actions during the outage never raises and keeps them all."""
        data = await self._nominal_then_down(
            degraded_hass, config_entry, setup_entry, fake_cloud, freezer, failure_mode
        )
        light_learning = data["light_learning"]

        context = Context(user_id=USER_ID)
        for state in ("on", "off"):
            old_state = degraded_hass.states.get(LIGHT_ID)
            degraded_hass.states.async_set(LIGHT_ID, state, context=context)
            new_state = degraded_hass.states.get(LIGHT_ID)
            result = await light_learning.capture_light_action(
                LIGHT_ID, new_state, old_state, context
            )
            assert result is None

        assert light_learning.get_pending_count() == 2
        # capture_light_action reports what it swallows as an ERROR on
        # utils.light_learning, which the guard does not exempt
        assert_no_unexpected_errors()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "The event listener drops light state changes: 'light' is not in "
            "get_monitored_domains(), so capture_light_action is never reached "
            "from the event bus. Production defect, out of scope of TK-57."
        ),
    )
    async def test_manual_light_change_reaches_the_capture_through_the_event_bus(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """A user switching the light on is buffered without any direct call."""
        data = await self._nominal_then_down(
            degraded_hass, config_entry, setup_entry, fake_cloud, freezer, failure_mode
        )

        degraded_hass.states.async_set(LIGHT_ID, "on", context=Context(user_id=USER_ID))
        await degraded_hass.async_block_till_done()

        assert data["light_learning"].get_pending_count() >= 1
        assert_no_unexpected_errors()


# ---------------------------------------------------------------------------
# Scenario 3: the database comes back
# ---------------------------------------------------------------------------


class TestCloudRecovery:
    """Return to nominal mode driven by the periodic probe alone."""

    pytestmark = pytest.mark.parametrize("failure_mode", FAILURE_MODES)

    async def test_probe_alone_detects_the_drop(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """In a quiet house the light probe is the only traffic that sees the outage."""
        assert await setup_entry("ok") is True
        mark = len(fake_cloud.journal)

        fake_cloud.mode = failure_mode
        await _advance_until(
            degraded_hass,
            freezer,
            lambda: _cloud_health(degraded_hass).state == "disconnected",
            max_ticks=2,
        )

        traffic = fake_cloud.journal[mark:]
        assert traffic, "the probe never reached the cloud"
        assert all(
            call["verb"] == "GET"
            and call["path"] == "ha_instances"
            and call["params"].get("limit") == "1"
            and call["served"] == failure_mode
            for call in traffic
        ), traffic
        assert _cloud_health(degraded_hass).attributes["is_degraded"] is True
        assert_no_unexpected_errors()

    async def test_nominal_mode_returns_without_intervention(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        hass_storage,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """Stale insights, pending actions and cloud_health all recover on their own."""
        hass = degraded_hass

        # Nominal setup persists the identity and the insights to local storage
        assert await setup_entry("ok") is True

        # The database falls, the probe notices, a manual action is buffered
        fake_cloud.mode = failure_mode
        await _advance(hass, freezer, RECOVERY_PROBE_INTERVAL)
        assert _cloud_health(hass).state == "disconnected"
        await _manual_light_action(
            hass, _entry_data(hass, config_entry)["light_learning"]
        )
        assert (
            _entry_data(hass, config_entry)["light_learning"].get_pending_count() >= 1
        )

        # Home Assistant restarts while the database is still down: insights
        # are restored from disk (the only way they become stale) and the
        # persisted buffer is reloaded
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert await setup_entry(failure_mode) is True

        data = _entry_data(hass, config_entry)
        insights = data["insights_manager"]
        light_learning = data["light_learning"]
        await _advance(hass, freezer, RECOVERY_PROBE_INTERVAL)

        assert fake_cloud.count(failure_mode) >= 1
        assert data["coordinator"].supabase_client.is_available() is False
        assert insights.is_stale() is True
        assert light_learning.get_pending_count() >= 1
        health = _cloud_health(hass)
        assert health.state == "disconnected"
        assert health.attributes["is_degraded"] is True
        assert health.attributes["insights_stale"] is True
        stale_value = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert stale_value is not None
        assert stale_value["value"] == {"threshold": INSIGHT_BEFORE}

        # The database comes back with fresher insights. From here on nothing
        # but the advance of time happens: no service call, no light event.
        fake_cloud.insight_threshold = INSIGHT_AFTER
        fake_cloud.mode = "ok"
        mark = len(fake_cloud.journal)
        service_calls: list[Any] = []
        entity_events: list[Any] = []
        hass.bus.async_listen(
            EVENT_CALL_SERVICE, lambda event: service_calls.append(event.data)
        )
        hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            lambda event: (
                entity_events.append(event.data["entity_id"])
                if event.data["entity_id"] in (LIGHT_ID, MOTION_ID)
                else None
            ),
        )

        ticks = await _advance_until(hass, freezer, lambda: not insights.is_stale())

        # Reprise within two probe intervals, as documented
        assert ticks <= 2

        # The first call that reached the cloud was the probe, not injected traffic
        recovery_traffic = fake_cloud.journal[mark:]
        assert recovery_traffic[0]["verb"] == "GET"
        assert recovery_traffic[0]["path"] == "ha_instances"
        assert recovery_traffic[0]["params"].get("limit") == "1"
        assert all(call["served"] == "ok" for call in recovery_traffic)
        assert [c for c in service_calls if c.get("domain") == DOMAIN] == []
        assert entity_events == []

        # Ordered: insights fresh, then the buffer drained, then nominal health
        assert insights.is_stale() is False
        refreshed = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert refreshed is not None
        assert refreshed["value"] == {"threshold": INSIGHT_AFTER}

        assert light_learning.get_pending_count() == 0
        assert len(fake_cloud.received_light_actions) >= 1
        persisted = hass_storage["linus_brain.light_actions"]["data"]["entries"]
        assert persisted == []

        health = _cloud_health(hass)
        assert health.state == "connected"
        assert health.attributes["is_degraded"] is False
        assert health.attributes["pending_light_actions"] == 0
        assert health.attributes["insights_stale"] is False
        assert_no_unexpected_errors()

    async def test_cold_start_in_outage_recovers_identity_and_insights(
        self,
        degraded_hass,
        config_entry,
        setup_entry,
        fake_cloud,
        freezer,
        assert_no_unexpected_errors,
        failure_mode,
    ):
        """A cold start without identity gets it, and the insights, on return."""
        assert await setup_entry(failure_mode) is True
        data = _entry_data(degraded_hass, config_entry)
        coordinator = data["coordinator"]
        insights = data["insights_manager"]
        assert coordinator.instance_id is None

        await _advance(degraded_hass, freezer, RECOVERY_PROBE_INTERVAL)
        assert _cloud_health(degraded_hass).state == "disconnected"
        assert insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE) is None

        fake_cloud.mode = "ok"
        ticks = await _advance_until(
            degraded_hass,
            freezer,
            lambda: _cloud_health(degraded_hass).state == "connected",
        )

        assert ticks <= 2
        assert coordinator.instance_id == INSTANCE_ID
        loaded = insights.get_insight(INSTANCE_ID, AREA_ID, INSIGHT_TYPE)
        assert loaded is not None
        assert loaded["value"] == {"threshold": INSIGHT_BEFORE}
        assert insights.is_stale() is False
        assert _cloud_health(degraded_hass).attributes["is_degraded"] is False
        assert_no_unexpected_errors()


# ---------------------------------------------------------------------------
# Scope contract and log guard
# ---------------------------------------------------------------------------


class TestDegradedScopeContract:
    """The document, the DEGRADED_SCOPE table and the tests tell the same story."""

    def test_document_section_lists_bullets(self):
        """The section parser finds bullets (a silent empty parse would pass vacuously)."""
        bullets = _doc_bullets(DOC_PATH.read_text(encoding="utf-8"))

        assert bullets
        assert all(bullet.strip() for bullet in bullets)

    def test_every_bullet_is_locked_by_an_existing_test(self):
        """No bullet without test, no unknown test in the table, same bullet count."""
        bullets = _doc_bullets(DOC_PATH.read_text(encoding="utf-8"))

        problems = _scope_problems(bullets, DEGRADED_SCOPE, sys.modules[__name__])

        assert problems == []

    def test_guard_flags_a_bullet_without_test(self):
        """A bullet added to the document without a table entry is reported."""
        problems = _scope_problems(
            [*DEGRADED_SCOPE, "Nouvelle puce"], DEGRADED_SCOPE, sys.modules[__name__]
        )

        assert any("bullet without test: Nouvelle puce" in p for p in problems)

    def test_guard_flags_a_test_that_does_not_exist(self):
        """A table entry pointing to a renamed or missing test is reported."""
        scope = {**DEGRADED_SCOPE, "Insights IA": "TestCloudRecovery::test_missing"}

        problems = _scope_problems(list(DEGRADED_SCOPE), scope, sys.modules[__name__])

        assert any("TestCloudRecovery::test_missing" in p for p in problems)

    def test_guard_flags_a_count_mismatch(self):
        """A document with one bullet fewer than the table is reported."""
        problems = _scope_problems(
            list(DEGRADED_SCOPE)[:-1], DEGRADED_SCOPE, sys.modules[__name__]
        )

        assert any("document counts" in p for p in problems)

    def test_log_guard_flags_an_error_of_another_module(self, caplog):
        """An ERROR from a non exempted module of the package is caught."""
        caplog.set_level(logging.WARNING, logger=LOGGER_ROOT)
        logging.getLogger(f"{LOGGER_ROOT}.utils.light_learning").error("boom")

        assert [r.name for r in _unexpected_errors(caplog)] == [
            f"{LOGGER_ROOT}.utils.light_learning"
        ]

    def test_log_guard_tolerates_exempt_modules_and_foreign_loggers(self, caplog):
        """The three exempt modules, and loggers outside the package, are ignored."""
        caplog.set_level(logging.WARNING, logger=LOGGER_ROOT)
        for name in EXEMPT_ERROR_LOGGERS:
            logging.getLogger(name).error("outage")
        logging.getLogger("homeassistant.core").error("not ours")
        logging.getLogger(f"{LOGGER_ROOT}x").error("sibling prefix, not a child")

        assert _unexpected_errors(caplog) == []
