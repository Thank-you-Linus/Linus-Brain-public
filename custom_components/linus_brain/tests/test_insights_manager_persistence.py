"""
Tests for InsightsManager local persistence.

Key responsibilities:
- Verify a successful cloud load persists raw insights and is not stale
- Verify a new manager restores insights from disk when the cloud is down
- Verify the memory-only (no store) behaviour is unchanged
- Verify an empty cloud answer overwrites the disk cache
- Verify a corrupted store never clears the memory cache
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from ..utils.insights_manager import STORAGE_KEY, STORAGE_VERSION, InsightsManager

INSTANCE_ID = "inst-123"


@pytest.fixture
def sample_insights() -> list[dict[str, Any]]:
    """Four raw insight rows covering the three fallback tiers."""
    return [
        {
            "id": "insight-1",
            "instance_id": INSTANCE_ID,
            "area_id": "salon",
            "insight_type": "dark_threshold_lux",
            "value": {"threshold": 150},
            "confidence": 0.9,
            "metadata": {"sample_size": 100},
            "updated_at": "2025-10-27T23:30:00Z",
        },
        {
            "id": "insight-2",
            "instance_id": None,
            "area_id": "cuisine",
            "insight_type": "dark_threshold_lux",
            "value": {"threshold": 180},
            "confidence": 0.7,
            "metadata": {},
            "updated_at": "2025-10-27T20:00:00Z",
        },
        {
            "id": "insight-3",
            "instance_id": None,
            "area_id": None,
            "insight_type": "dark_threshold_lux",
            "value": {"threshold": 200},
            "confidence": 0.5,
            "metadata": {},
            "updated_at": "2025-10-27T18:00:00Z",
        },
        {
            "id": "insight-4",
            "instance_id": None,
            "area_id": None,
            "insight_type": "dark_mode_brightness_pct",
            "value": {"brightness_pct": 30},
            "confidence": 0.5,
            "metadata": {},
            "updated_at": "2025-10-27T18:00:00Z",
        },
    ]


def _make_client(return_value: Any) -> MagicMock:
    """Build a fake SupabaseClient whose fetch_area_insights returns a value."""
    client = MagicMock()
    client.fetch_area_insights = AsyncMock(return_value=return_value)
    return client


async def _make_manager(hass: HomeAssistant, return_value: Any) -> InsightsManager:
    """Build an InsightsManager with a store attached."""
    manager = InsightsManager(_make_client(return_value))
    await manager.async_setup_store(hass)
    return manager


async def test_successful_load_persists_and_is_not_stale(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """A successful cloud load persists raw rows and is flagged fresh."""
    manager = await _make_manager(hass, sample_insights)

    assert await manager.async_load(INSTANCE_ID) is True
    assert manager.is_stale() is False
    assert manager.loaded_from_cache is False

    # Read the store back through a second manager with the cloud down
    reader = await _make_manager(hass, None)
    assert await reader._async_restore_from_cache() is True
    assert reader._cache == manager._cache

    assert reader._store is not None
    persisted = await reader._store.async_load()
    assert persisted is not None
    assert persisted["version"] == STORAGE_VERSION
    assert persisted["insights"] == sample_insights
    assert persisted["saved_at"]


async def test_new_instance_restores_from_disk_when_cloud_down(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """A fresh manager with a store restores the cache when the cloud is down."""
    manager_a = await _make_manager(hass, sample_insights)
    assert await manager_a.async_load(INSTANCE_ID) is True
    assert len(manager_a._cache) == 4

    manager_b = await _make_manager(hass, None)
    result = await manager_b.async_load(INSTANCE_ID)

    # The public contract stays "True = the cloud answered"
    assert result is False
    assert len(manager_b._cache) == 4
    assert manager_b.loaded_from_cache is True
    assert manager_b.is_stale() is True
    assert manager_b.is_loaded() is True
    insight = manager_b.get_insight(INSTANCE_ID, "salon", "dark_threshold_lux")
    assert insight is not None
    assert insight["value"] == {"threshold": 150}


async def test_no_store_keeps_legacy_behaviour(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """Without a store attached, a cloud failure leaves an empty cache."""
    manager_a = await _make_manager(hass, sample_insights)
    assert await manager_a.async_load(INSTANCE_ID) is True

    manager_b = InsightsManager(_make_client(None))
    result = await manager_b.async_load(INSTANCE_ID)

    assert result is False
    assert len(manager_b._cache) == 0
    assert manager_b.loaded_from_cache is False
    assert manager_b.is_stale() is False


async def test_empty_cloud_response_overwrites_disk_cache(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """An empty cloud answer is a success and overwrites the disk cache."""
    manager_a = await _make_manager(hass, sample_insights)
    assert await manager_a.async_load(INSTANCE_ID) is True

    manager_empty = await _make_manager(hass, [])
    assert await manager_empty.async_load(INSTANCE_ID) is True
    assert manager_empty.is_stale() is False

    assert manager_empty._store is not None
    persisted = await manager_empty._store.async_load()
    assert persisted is not None
    assert persisted["insights"] == []

    manager_down = await _make_manager(hass, None)
    assert await manager_down.async_load(INSTANCE_ID) is False
    assert len(manager_down._cache) == 0
    assert manager_down.is_stale() is True


async def test_corrupted_store_does_not_clear_memory_cache(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    sample_insights: list[dict[str, Any]],
) -> None:
    """An invalid persisted payload never clears the memory cache."""
    manager = await _make_manager(hass, sample_insights)
    assert await manager.async_load(INSTANCE_ID) is True
    assert len(manager._cache) == 4

    # Corrupt the persisted payload
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"version": 999, "insights": "not-a-list"},
    }

    manager.supabase_client.fetch_area_insights = AsyncMock(return_value=None)
    assert await manager.async_load(INSTANCE_ID) is False
    assert await manager._async_restore_from_cache() is False

    assert len(manager._cache) == 4
    assert manager.loaded_from_cache is False
    assert manager.is_stale() is False


async def test_cloud_outage_keeps_memory_over_older_disk_copy(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    sample_insights: list[dict[str, Any]],
) -> None:
    """A cloud outage never overwrites fresher in-memory insights from disk."""
    manager = await _make_manager(hass, sample_insights)
    assert await manager.async_load(INSTANCE_ID) is True

    # Simulate a silently failed persist: the disk holds an older, smaller copy
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "version": STORAGE_VERSION,
            "insights": sample_insights[:1],
            "saved_at": "2025-01-01T00:00:00+00:00",
        },
    }

    manager.supabase_client.fetch_area_insights = AsyncMock(return_value=None)
    assert await manager.async_load(INSTANCE_ID) is False
    assert len(manager._cache) == 4
    assert manager.loaded_from_cache is False
    assert manager.is_stale() is False

    # Same guarantee when the fetch raises (exception handler path)
    manager.supabase_client.fetch_area_insights = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    assert await manager.async_load(INSTANCE_ID) is False
    assert len(manager._cache) == 4
    assert manager.loaded_from_cache is False
    assert manager.is_stale() is False


async def test_exception_with_empty_cache_restores_from_disk(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """An exception on a manager with an empty cache falls back to the disk."""
    manager_a = await _make_manager(hass, sample_insights)
    assert await manager_a.async_load(INSTANCE_ID) is True

    manager_b = await _make_manager(hass, None)
    manager_b.supabase_client.fetch_area_insights = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    assert await manager_b.async_load(INSTANCE_ID) is False
    assert len(manager_b._cache) == 4
    assert manager_b.loaded_from_cache is True
    assert manager_b.is_stale() is True


async def test_failed_populate_leaves_previous_cache_intact(
    hass: HomeAssistant, sample_insights: list[dict[str, Any]]
) -> None:
    """A malformed cloud payload never leaves the live cache half-built."""
    manager = await _make_manager(hass, sample_insights)
    assert await manager.async_load(INSTANCE_ID) is True
    before = dict(manager._cache)

    manager.supabase_client.fetch_area_insights = AsyncMock(
        return_value=[sample_insights[0], "not-a-dict"]
    )
    assert await manager.async_load(INSTANCE_ID) is False
    assert manager._cache == before
    assert manager.loaded_from_cache is False
