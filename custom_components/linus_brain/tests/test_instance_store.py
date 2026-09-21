"""
Unit tests for InstanceStore.

Tests the persistent storage of the cloud instance identity:
- Empty load when nothing is persisted
- Save then load round-trip
- Refusal to persist a falsy instance_id
- Corruption recovery (load/save never raise)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..utils import instance_store as instance_store_module
from ..utils.instance_store import STORAGE_KEY, STORAGE_VERSION, InstanceStore


@pytest.fixture
def mock_store():
    """Mock the Home Assistant Store helper used by InstanceStore."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock(return_value=None)
    return store


@pytest.fixture
def instance_store(mock_store):
    """Create an InstanceStore backed by a mocked Store."""
    with patch.object(instance_store_module, "Store", return_value=mock_store):
        return InstanceStore(MagicMock())


class TestInstanceStoreLoad:
    """Tests for InstanceStore.async_load()."""

    async def test_load_returns_empty_when_nothing_persisted(
        self, instance_store, mock_store
    ):
        """An empty store yields an empty identity, not an exception."""
        mock_store.async_load.return_value = None

        identity = await instance_store.async_load()

        assert identity == {}

    async def test_load_returns_persisted_identity(self, instance_store, mock_store):
        """A persisted payload is returned with both identity fields."""
        mock_store.async_load.return_value = {
            "version": STORAGE_VERSION,
            "instance_id": "instance-abc",
            "ha_installation_id": "ha-uuid-1",
        }

        identity = await instance_store.async_load()

        assert identity["instance_id"] == "instance-abc"
        assert identity["ha_installation_id"] == "ha-uuid-1"

    async def test_load_ignores_payload_without_instance_id(
        self, instance_store, mock_store
    ):
        """A payload missing instance_id is treated as no identity."""
        mock_store.async_load.return_value = {
            "version": STORAGE_VERSION,
            "ha_installation_id": "ha-uuid-1",
        }

        identity = await instance_store.async_load()

        assert identity == {}

    async def test_load_recovers_from_corrupted_store(self, instance_store, mock_store):
        """A corrupted store returns {} instead of raising."""
        mock_store.async_load.side_effect = ValueError("corrupted JSON")

        identity = await instance_store.async_load()

        assert identity == {}


class TestInstanceStoreSave:
    """Tests for InstanceStore.async_save()."""

    async def test_save_then_load_round_trip(self, instance_store, mock_store):
        """Saving writes the expected payload, which loads back identically."""
        saved = await instance_store.async_save("instance-abc", "ha-uuid-1")

        assert saved is True
        payload = mock_store.async_save.call_args[0][0]
        assert payload["instance_id"] == "instance-abc"
        assert payload["ha_installation_id"] == "ha-uuid-1"
        assert payload["version"] == STORAGE_VERSION

        mock_store.async_load.return_value = payload
        identity = await instance_store.async_load()

        assert identity["instance_id"] == "instance-abc"
        assert identity["ha_installation_id"] == "ha-uuid-1"

    async def test_save_refuses_empty_instance_id(self, instance_store, mock_store):
        """A falsy instance_id is never persisted."""
        saved = await instance_store.async_save("", "ha-uuid-1")

        assert saved is False
        mock_store.async_save.assert_not_called()

    async def test_save_returns_false_on_storage_error(
        self, instance_store, mock_store
    ):
        """A storage failure returns False instead of raising."""
        mock_store.async_save.side_effect = OSError("disk full")

        saved = await instance_store.async_save("instance-abc", "ha-uuid-1")

        assert saved is False


class TestInstanceStoreConfiguration:
    """Tests for the storage key/version contract."""

    def test_storage_key_is_domain_scoped(self):
        """The storage key is namespaced under the integration domain."""
        assert STORAGE_KEY == "linus_brain.instance"
        assert STORAGE_VERSION == 1
