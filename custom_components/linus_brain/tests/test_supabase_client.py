"""
Unit tests for SupabaseClient.

Tests the HTTP return contract:
- Every read returns None when Supabase is unavailable (status != 200)
- Every read returns [] or {} when the cloud answers 200 with zero row
- Every write returns False when Supabase is unavailable
- All HTTP traffic goes through the three _http_* helpers

And the availability contract:
- A network failure (timeout, ClientError) never propagates
- is_available() follows the last exchange
- The circuit breaker stops the I/O once the backend is down
- An unreachable backend logs no ERROR record
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from freezegun import freeze_time

from ..utils import supabase_client as supabase_client_module
from ..utils.supabase_client import (
    CIRCUIT_COOLDOWN_SECONDS,
    FAILURE_THRESHOLD,
    SupabaseClient,
)

LOGGER_UNDER_TEST = "custom_components.linus_brain"

# The two ways an unreachable backend shows up inside the helpers. A timeout
# is the dominant one: it is the builtin TimeoutError and never passes through
# aiohttp.ClientError.
NETWORK_FAILURES = [TimeoutError, aiohttp.ClientError]


class _FakeResponse:
    """Minimal aiohttp response double usable as an async context manager."""

    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self):
        """Return the decoded JSON payload."""
        return self._payload

    async def text(self):
        """Return the raw body."""
        return self._text

    async def __aenter__(self):
        """Enter the async context."""
        return self

    async def __aexit__(self, *exc):
        """Leave the async context without swallowing exceptions."""
        return False


class _UndecodableResponse(_FakeResponse):
    """Response that answers 200 but whose body is not valid JSON."""

    async def json(self):
        """Fail like aiohttp does on an unparsable body."""
        raise ValueError("unparsable body")


def _make_client(gets=None, posts=None, patches=None) -> SupabaseClient:
    """
    Build a SupabaseClient whose aiohttp session is fully mocked.

    Args:
        gets: Responses returned by successive session.get() calls
        posts: Responses returned by successive session.post() calls
        patches: Responses returned by successive session.patch() calls

    Returns:
        A client that never touches the network
    """
    with patch.object(
        supabase_client_module, "async_get_clientsession", return_value=MagicMock()
    ):
        client = SupabaseClient(MagicMock(), "https://demo.supabase.co", "test-key")

    session = MagicMock()
    session.get = MagicMock(side_effect=list(gets or []))
    session.post = MagicMock(side_effect=list(posts or []))
    session.patch = MagicMock(side_effect=list(patches or []))
    client.session = session
    return client


def _break_network(client: SupabaseClient, failure: type[Exception]) -> None:
    """
    Make every HTTP verb of a client raise the given network failure.

    Args:
        client: Client built by _make_client()
        failure: Exception class raised at call time, inside the helper's try
    """
    client.session.get = MagicMock(side_effect=failure())
    client.session.post = MagicMock(side_effect=failure())
    client.session.patch = MagicMock(side_effect=failure())


# Every read method, with the arguments it needs
READ_METHODS = [
    ("fetch_rules", ()),
    ("fetch_rules_for_instance", ("inst-123",)),
    ("get_rule_for_area", ("inst-123", "salon")),
    ("fetch_activity_types", ()),
    ("fetch_app_with_actions", ("automatic_lighting",)),
    ("fetch_area_insights", ("inst-123",)),
    ("get_instance_by_ha_id", ("ha-uuid",)),
]


class TestHelperRouting:
    """Test that no method builds its own aiohttp call."""

    def test_only_helpers_touch_the_session(self):
        """Test that self.session is used in the 3 helpers only."""
        source = Path(supabase_client_module.__file__).read_text(encoding="utf-8")

        # _http_get, _http_post, _http_patch - and nothing else
        assert source.count("self.session.") == 3


class TestUnavailableBackend:
    """Test the 'Supabase unavailable' branch of the return contract."""

    @pytest.mark.parametrize("method_name,args", READ_METHODS)
    async def test_read_returns_none_on_503(self, method_name, args):
        """Test that a 503 makes every read return None, never [] or {}."""
        client = _make_client(
            gets=[_FakeResponse(503, text="unavailable") for _ in range(3)],
            posts=[_FakeResponse(503, text="unavailable") for _ in range(3)],
        )

        result = await getattr(client, method_name)(*args)

        assert result is None

    async def test_send_light_action_returns_false_on_503(self):
        """Test that a 503 makes send_light_action return False."""
        client = _make_client(posts=[_FakeResponse(503, text="unavailable")])

        assert await client.send_light_action({"entity_id": "light.salon"}) is False

    async def test_update_instance_last_seen_returns_false_on_503(self):
        """Test that a 503 makes update_instance_last_seen return False."""
        client = _make_client(patches=[_FakeResponse(503, text="unavailable")])

        assert await client.update_instance_last_seen("inst-123") is False

    async def test_push_rules_returns_false_on_503(self):
        """Test that a 503 makes push_rules_for_instance return False."""
        client = _make_client(posts=[_FakeResponse(503, text="unavailable")])

        rules = [
            {
                "area_id": "salon",
                "area_name": "Salon",
                "activity_rules": {"movement": {"conditions": [], "actions": []}},
            }
        ]

        assert await client.push_rules_for_instance("inst-123", rules) is False

    async def test_push_rules_returns_false_on_partial_success(self):
        """Test that a partially pushed batch is reported as a failure."""
        client = _make_client(
            posts=[_FakeResponse(201, payload={}), _FakeResponse(503, text="down")]
        )

        rules = [
            {
                "area_id": "salon",
                "area_name": "Salon",
                "activity_rules": {
                    "movement": {"conditions": [], "actions": []},
                    "empty": {"conditions": [], "actions": []},
                },
            }
        ]

        assert await client.push_rules_for_instance("inst-123", rules) is False

    async def test_create_new_instance_returns_none_on_503(self):
        """Test that a 503 makes create_new_instance return None."""
        client = _make_client(posts=[_FakeResponse(503, text="unavailable")])

        assert await client.create_new_instance("ha-uuid") is None


class TestEmptyCloudAnswer:
    """Test the '200 with zero row' branch of the return contract."""

    @pytest.mark.parametrize("method_name,args", READ_METHODS)
    async def test_read_returns_empty_container_on_200(self, method_name, args):
        """Test that an empty 200 answer returns [] or {}, never None."""
        client = _make_client(
            gets=[_FakeResponse(200, payload=[]) for _ in range(3)],
            # Empty RPC answer = app without published version
            posts=[_FakeResponse(200, payload="") for _ in range(3)],
        )

        result = await getattr(client, method_name)(*args)

        assert result is not None
        assert result in ([], {})

    async def test_get_instance_by_ha_id_unknown_instance_returns_empty_dict(self):
        """Test that an unknown installation returns {} and not None."""
        client = _make_client(gets=[_FakeResponse(200, payload=[])])

        assert await client.get_instance_by_ha_id("ha-uuid") == {}

    async def test_get_rule_for_area_without_rule_returns_empty_dict(self):
        """Test that an area without rule returns {} and not None."""
        client = _make_client(gets=[_FakeResponse(200, payload=[])])

        assert await client.get_rule_for_area("inst-123", "salon") == {}

    async def test_get_instance_by_ha_id_returns_first_row(self):
        """Test that a known installation returns its instance row."""
        client = _make_client(
            gets=[_FakeResponse(200, payload=[{"instance_id": "inst-123"}])]
        )

        assert await client.get_instance_by_ha_id("ha-uuid") == {
            "instance_id": "inst-123"
        }


class TestFetchAppWithActions:
    """Test the three exit points of fetch_app_with_actions."""

    async def test_rpc_unavailable_returns_none(self):
        """Test that a 503 on the version RPC returns None."""
        client = _make_client(posts=[_FakeResponse(503, text="down")])

        assert await client.fetch_app_with_actions("automatic_lighting") is None

    async def test_no_published_version_returns_empty_dict(self):
        """Test that an app without published version returns {}."""
        client = _make_client(posts=[_FakeResponse(200, payload=None)])

        assert await client.fetch_app_with_actions("automatic_lighting") == {}

    async def test_app_table_unavailable_returns_none(self):
        """Test that a 503 on the app table returns None."""
        client = _make_client(
            posts=[_FakeResponse(200, payload="2025-10-26T12:00:00Z")],
            gets=[_FakeResponse(503, text="down")],
        )

        assert await client.fetch_app_with_actions("automatic_lighting") is None

    async def test_app_absent_returns_empty_dict(self):
        """Test that an app absent from the cloud returns {}."""
        client = _make_client(
            posts=[_FakeResponse(200, payload="2025-10-26T12:00:00Z")],
            gets=[_FakeResponse(200, payload=[])],
        )

        assert await client.fetch_app_with_actions("automatic_lighting") == {}

    async def test_actions_unavailable_returns_none(self):
        """
        Test that a 503 on app_activity_actions alone returns None.

        The app itself answers 200: returning it without its actions would
        write a dead automatic_lighting into the local cache.
        """
        client = _make_client(
            posts=[_FakeResponse(200, payload="2025-10-26T12:00:00Z")],
            gets=[
                _FakeResponse(200, payload=[{"app_id": "automatic_lighting"}]),
                _FakeResponse(503, text="down"),
            ],
        )

        result = await client.fetch_app_with_actions("automatic_lighting")

        assert result is None

    async def test_full_app_is_returned_with_its_actions(self):
        """Test the nominal path: app + activity actions."""
        client = _make_client(
            posts=[_FakeResponse(200, payload="2025-10-26T12:00:00Z")],
            gets=[
                _FakeResponse(200, payload=[{"app_id": "automatic_lighting"}]),
                _FakeResponse(
                    200,
                    payload=[
                        {
                            "activity_id": "movement",
                            "conditions": [],
                            "actions": [{"service": "light.turn_on"}],
                        }
                    ],
                ),
            ],
        )

        result = await client.fetch_app_with_actions("automatic_lighting")

        assert result is not None
        assert "movement" in result["activity_actions"]


class TestTestConnection:
    """Test the connection check used by the config flow."""

    @pytest.mark.parametrize("status", [200, 401, 404])
    async def test_reachable_backend_is_a_success(self, status, caplog):
        """Test that 200/401/404 are successes and log no error."""
        client = _make_client(gets=[_FakeResponse(status, payload={}, text="")])

        assert await client.test_connection() is True
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    async def test_unreachable_backend_is_a_failure(self):
        """Test that a 503 fails the connection check."""
        client = _make_client(gets=[_FakeResponse(503, text="down")])

        assert await client.test_connection() is False


class TestNetworkFailureIsNotAnException:
    """Test that an unreachable backend never propagates out of the client."""

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    @pytest.mark.parametrize("method_name,args", READ_METHODS)
    async def test_read_returns_none_on_network_failure(
        self, method_name, args, failure
    ):
        """Test that every read returns None instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        assert await getattr(client, method_name)(*args) is None

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    async def test_send_light_action_returns_false_on_network_failure(self, failure):
        """Test that send_light_action returns False instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        assert await client.send_light_action({"entity_id": "light.salon"}) is False

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    async def test_update_instance_last_seen_returns_false_on_network_failure(
        self, failure
    ):
        """Test that update_instance_last_seen returns False instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        assert await client.update_instance_last_seen("inst-123") is False

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    async def test_push_rules_returns_false_on_network_failure(self, failure):
        """Test that push_rules_for_instance returns False instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        rules = [
            {
                "area_id": "salon",
                "area_name": "Salon",
                "activity_rules": {"movement": {"conditions": [], "actions": []}},
            }
        ]

        assert await client.push_rules_for_instance("inst-123", rules) is False

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    async def test_create_new_instance_returns_none_on_network_failure(self, failure):
        """Test that create_new_instance returns None instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        assert await client.create_new_instance("ha-uuid") is None

    @pytest.mark.parametrize("failure", NETWORK_FAILURES)
    async def test_test_connection_returns_false_on_network_failure(self, failure):
        """Test that the config flow check returns False instead of raising."""
        client = _make_client()
        _break_network(client, failure)

        assert await client.test_connection() is False


class TestAvailabilityState:
    """Test is_available(), the single owner of 'the backend is down'."""

    async def test_a_fresh_client_is_available(self):
        """Test that a client starts optimistic."""
        assert _make_client().is_available() is True

    async def test_failure_then_success_flips_availability_back(self):
        """Test the full cycle on a single client: down, then up again."""
        client = _make_client()
        client.session.get = MagicMock(side_effect=TimeoutError())

        assert await client.fetch_rules() is None
        assert client.is_available() is False

        client.session.get = MagicMock(side_effect=[_FakeResponse(200, payload=[])])

        assert await client.fetch_rules() == []
        assert client.is_available() is True

    async def test_server_error_marks_the_backend_unavailable(self):
        """Test that a 5xx counts as an unavailability."""
        client = _make_client(gets=[_FakeResponse(503, text="down")])

        assert await client.fetch_rules() is None
        assert client.is_available() is False

    async def test_client_error_status_keeps_the_backend_available(self):
        """Test that a 4xx means the backend answered, so it is reachable."""
        client = _make_client(gets=[_FakeResponse(400, text="bad request")])

        assert await client.fetch_rules() is None
        assert client.is_available() is True

    async def test_undecodable_body_does_not_open_the_circuit(self):
        """Test that a client-side bug is not an unavailability."""
        client = _make_client(gets=[_UndecodableResponse(200)])

        assert await client.fetch_rules() is None
        assert client.is_available() is True
        assert client._consecutive_failures == 0


class TestCircuitBreaker:
    """Test that a down backend stops costing one timeout per call."""

    async def test_open_circuit_returns_unavailability_without_any_io(self):
        """Test that the circuit opens after N failures, then closes again."""
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

        with freeze_time(start) as frozen_time:
            client = _make_client()
            _break_network(client, TimeoutError)

            for _ in range(FAILURE_THRESHOLD):
                assert await client.fetch_rules() is None

            calls_when_opened = client.session.get.call_count
            assert calls_when_opened == FAILURE_THRESHOLD

            # Circuit open: reads and writes answer from memory only
            assert await client.fetch_rules() is None
            assert await client.get_instance_by_ha_id("ha-uuid") is None
            assert await client.send_light_action({"entity_id": "light.salon"}) is False
            assert await client.update_instance_last_seen("inst-123") is False

            assert client.session.get.call_count == calls_when_opened
            client.session.post.assert_not_called()
            client.session.patch.assert_not_called()

            # The first call after the window IS the probe
            frozen_time.tick(delta=timedelta(seconds=CIRCUIT_COOLDOWN_SECONDS + 1))
            client.session.get = MagicMock(side_effect=[_FakeResponse(200, payload=[])])

            assert await client.fetch_rules() == []
            assert client.session.get.call_count == 1
            assert client.is_available() is True

    async def test_a_failing_probe_opens_a_new_window(self):
        """Test that the circuit never stays open, and never closes on a failure."""
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

        with freeze_time(start) as frozen_time:
            client = _make_client()
            _break_network(client, TimeoutError)

            for _ in range(FAILURE_THRESHOLD):
                await client.fetch_rules()

            frozen_time.tick(delta=timedelta(seconds=CIRCUIT_COOLDOWN_SECONDS + 1))

            # The probe goes out and fails: a new window starts
            assert await client.fetch_rules() is None
            assert client.session.get.call_count == FAILURE_THRESHOLD + 1

            assert await client.fetch_rules() is None
            assert client.session.get.call_count == FAILURE_THRESHOLD + 1

    async def test_test_connection_bypasses_the_open_circuit(self):
        """Test that the config flow always reaches the network, and closes it."""
        start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

        with freeze_time(start):
            client = _make_client()
            _break_network(client, TimeoutError)

            for _ in range(FAILURE_THRESHOLD):
                await client.fetch_rules()

            # 401 = the backend answered: the check succeeds and the state
            # goes back to available even though the circuit was open.
            client.session.get = MagicMock(side_effect=[_FakeResponse(401, text="")])

            assert await client.test_connection() is True
            assert client.session.get.call_count == 1
            assert client.is_available() is True


class TestPartialPushIsNotAnUnavailability:
    """Test the only write whose False can mean something else than 'down'."""

    async def test_partial_push_on_a_4xx_keeps_the_backend_available(self):
        """Test that a rejected row fails the push without flagging an outage."""
        client = _make_client(
            posts=[_FakeResponse(201, payload={}), _FakeResponse(400, text="rejected")]
        )

        rules = [
            {
                "area_id": "salon",
                "area_name": "Salon",
                "activity_rules": {
                    "movement": {"conditions": [], "actions": []},
                    "empty": {"conditions": [], "actions": []},
                },
            }
        ]

        assert await client.push_rules_for_instance("inst-123", rules) is False
        assert client.is_available() is True


class TestNoErrorLogWhenTheBackendIsDown:
    """Test that a full outage is reported as a warning, never as an error."""

    async def test_outage_scenario_logs_no_error_record(self, caplog):
        """Test timeout, ClientError and 503 across the three verbs."""
        caplog.set_level(logging.WARNING, logger=LOGGER_UNDER_TEST)

        client = _make_client()
        _break_network(client, TimeoutError)
        await client.fetch_rules()
        await client.send_light_action({"entity_id": "light.salon"})
        await client.update_instance_last_seen("inst-123")

        client = _make_client()
        _break_network(client, aiohttp.ClientError)
        await client.fetch_rules()
        await client.send_light_action({"entity_id": "light.salon"})
        await client.update_instance_last_seen("inst-123")

        client = _make_client(
            gets=[_FakeResponse(503, text="down")],
            posts=[_FakeResponse(503, text="down")],
            patches=[_FakeResponse(503, text="down")],
        )
        await client.fetch_rules()
        await client.send_light_action({"entity_id": "light.salon"})
        await client.update_instance_last_seen("inst-123")

        errors = [
            record
            for record in caplog.records
            if record.levelname == "ERROR" and record.name.startswith(LOGGER_UNDER_TEST)
        ]

        assert errors == []
