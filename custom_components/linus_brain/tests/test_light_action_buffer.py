"""
Unit tests for LightActionBuffer.

Tests the local buffering of unacknowledged light actions:
- Enqueue persistence and FIFO bound
- Load (empty, persisted, corrupted store)
- Replay semantics (ack-only removal, attempts cap, no blocking, re-entrancy)
- Synchronous, side-effect free pending counter
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..const import (
    LIGHT_ACTION_BUFFER_MAX_ATTEMPTS,
    LIGHT_ACTION_BUFFER_MAX_ENTRIES,
)
from ..utils import light_action_buffer as light_action_buffer_module
from ..utils.light_action_buffer import STORAGE_VERSION, LightActionBuffer


@pytest.fixture
def mock_store():
    """Mock the Home Assistant Store helper used by LightActionBuffer."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock(return_value=None)
    return store


@pytest.fixture
def buffer(mock_store):
    """Create a LightActionBuffer backed by a mocked Store."""
    with patch.object(light_action_buffer_module, "Store", return_value=mock_store):
        return LightActionBuffer(MagicMock())


def _payload(n: int) -> dict:
    return {"context_id": f"ctx-{n}", "entity_id": "light.kitchen", "n": n}


class TestLoad:
    """Tests for async_load()."""

    async def test_load_empty_store(self, buffer):
        """Nothing persisted yields an empty queue."""
        await buffer.async_load()
        assert buffer.get_pending_count() == 0

    async def test_load_persisted_entries(self, buffer, mock_store):
        """Persisted entries are restored."""
        mock_store.async_load.return_value = {
            "version": STORAGE_VERSION,
            "entries": [
                {"id": "a", "payload": _payload(1), "attempts": 2, "queued_at": "x"}
            ],
        }
        await buffer.async_load()
        assert buffer.get_pending_count() == 1

    async def test_load_corrupted_store_gives_empty_queue(self, buffer, mock_store):
        """A store that raises never propagates and yields an empty queue."""
        mock_store.async_load.side_effect = ValueError("corrupted")
        await buffer.async_load()
        assert buffer.get_pending_count() == 0

    async def test_load_is_idempotent(self, buffer, mock_store):
        """A second load does not hit the store again."""
        await buffer.async_load()
        await buffer.async_load()
        assert mock_store.async_load.await_count == 1


class TestEnqueue:
    """Tests for async_enqueue()."""

    async def test_enqueue_persists_entry(self, buffer, mock_store):
        """An enqueued payload is persisted with id, attempts and queued_at."""
        assert await buffer.async_enqueue(_payload(1)) is True

        saved = mock_store.async_save.call_args[0][0]
        assert saved["version"] == STORAGE_VERSION
        assert len(saved["entries"]) == 1
        entry = saved["entries"][0]
        assert entry["id"] == "ctx-1"
        assert entry["payload"] == _payload(1)
        assert entry["attempts"] == 0
        assert entry["queued_at"]

    async def test_enqueue_without_context_id_generates_id(self, buffer, mock_store):
        """A payload without context_id still gets a unique entry id."""
        await buffer.async_enqueue({"entity_id": "light.a"})
        entry = mock_store.async_save.call_args[0][0]["entries"][0]
        assert entry["id"]

    async def test_queue_is_bounded_fifo(self, buffer, mock_store):
        """The 101st entry evicts the oldest; size stays constant."""
        for n in range(LIGHT_ACTION_BUFFER_MAX_ENTRIES + 1):
            await buffer.async_enqueue(_payload(n))

        assert buffer.get_pending_count() == LIGHT_ACTION_BUFFER_MAX_ENTRIES
        saved = mock_store.async_save.call_args[0][0]["entries"]
        assert len(saved) == LIGHT_ACTION_BUFFER_MAX_ENTRIES
        assert saved[0]["payload"]["n"] == 1
        assert saved[-1]["payload"]["n"] == LIGHT_ACTION_BUFFER_MAX_ENTRIES

    async def test_enqueue_never_raises_on_save_failure(self, buffer, mock_store):
        """A failing store does not propagate."""
        mock_store.async_save.side_effect = OSError("disk full")
        assert await buffer.async_enqueue(_payload(1)) is False


class TestPendingCount:
    """Tests for get_pending_count()."""

    def test_is_synchronous(self, buffer):
        """The accessor is a plain function, not a coroutine function."""
        assert not asyncio.iscoroutinefunction(buffer.get_pending_count)
        assert buffer.get_pending_count() == 0

    async def test_counts_without_side_effects(self, buffer, mock_store):
        """Count reflects the queue and never touches the persisted content."""
        for n in range(3):
            await buffer.async_enqueue(_payload(n))
        saves = mock_store.async_save.call_count

        assert buffer.get_pending_count() == 3
        assert buffer.get_pending_count() == 3
        assert mock_store.async_save.call_count == saves
        assert isinstance(buffer.get_pending_count(), int)

    async def test_zero_after_full_replay(self, buffer):
        """All entries acknowledged leaves nothing pending."""
        for n in range(3):
            await buffer.async_enqueue(_payload(n))

        replayed = await buffer.async_replay_pending(AsyncMock(return_value=True))

        assert replayed == 3
        assert buffer.get_pending_count() == 0


class TestReplay:
    """Tests for async_replay_pending()."""

    async def test_refused_entry_is_kept_and_attempts_incremented(
        self, buffer, mock_store
    ):
        """An entry is removed only on ack: False keeps it, attempts + 1."""
        await buffer.async_enqueue(_payload(1))

        replayed = await buffer.async_replay_pending(AsyncMock(return_value=False))

        assert replayed == 0
        assert buffer.get_pending_count() == 1
        saved = mock_store.async_save.call_args[0][0]["entries"]
        assert saved[0]["attempts"] == 1

    async def test_raising_send_is_treated_as_refusal(self, buffer):
        """An exception from send keeps the entry and does not propagate."""
        await buffer.async_enqueue(_payload(1))

        replayed = await buffer.async_replay_pending(
            AsyncMock(side_effect=RuntimeError("boom"))
        )

        assert replayed == 0
        assert buffer.get_pending_count() == 1

    async def test_refused_entry_does_not_block_following(self, buffer):
        """Later entries are sent in the same replay despite a refused one."""
        for n in range(3):
            await buffer.async_enqueue(_payload(n))

        async def send(payload):
            return payload["n"] != 0

        replayed = await buffer.async_replay_pending(send)

        assert replayed == 2
        assert buffer.get_pending_count() == 1

    async def test_entry_discarded_at_attempt_cap(self, buffer, caplog):
        """A systematically refused entry is dropped at the cap with a warning."""
        await buffer.async_enqueue(_payload(1))
        send = AsyncMock(return_value=False)

        for _ in range(LIGHT_ACTION_BUFFER_MAX_ATTEMPTS - 1):
            await buffer.async_replay_pending(send)
        assert buffer.get_pending_count() == 1

        with caplog.at_level("WARNING"):
            await buffer.async_replay_pending(send)

        assert buffer.get_pending_count() == 0
        assert "Discarding light action" in caplog.text

    async def test_concurrent_replays_send_each_entry_once(self, buffer):
        """Two concurrent replays over N entries call send exactly N times."""
        n_entries = 5
        for n in range(n_entries):
            await buffer.async_enqueue(_payload(n))

        async def send(payload):
            await asyncio.sleep(0)
            return True

        mock_send = AsyncMock(side_effect=send)

        results = await asyncio.gather(
            buffer.async_replay_pending(mock_send),
            buffer.async_replay_pending(mock_send),
        )

        assert mock_send.await_count == n_entries
        assert sorted(results) == [0, n_entries]
        assert buffer.get_pending_count() == 0

    async def test_replay_flag_reset_after_exception_in_save(self, buffer, mock_store):
        """The re-entrancy flag is released even if the replay fails midway."""
        await buffer.async_enqueue(_payload(1))
        mock_store.async_save.side_effect = OSError("disk full")

        await buffer.async_replay_pending(AsyncMock(return_value=True))

        mock_store.async_save.side_effect = None
        await buffer.async_enqueue(_payload(2))
        assert await buffer.async_replay_pending(AsyncMock(return_value=True)) == 1
