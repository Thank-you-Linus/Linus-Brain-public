"""
Unit tests for SupabaseClient.

Tests the HTTP return contract:
- Every read returns None when Supabase is unavailable (status != 200)
- Every read returns [] or {} when the cloud answers 200 with zero row
- Every write returns False when Supabase is unavailable
- All HTTP traffic goes through the three _http_* helpers
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ..utils import supabase_client as supabase_client_module
from ..utils.supabase_client import SupabaseClient


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
