"""
Local buffer for learned light actions awaiting delivery to the cloud.

When the cloud did not acknowledge a light action (send returned False or
raised), the payload is kept here and replayed once the cloud is back, so a
database outage does not lose learning data.

Key responsibilities:
- Persist unacknowledged payloads through Home Assistant's Store helper
- Keep a bounded FIFO queue (oldest entries evicted first)
- Replay pending entries through a single, re-entrancy-safe method
- Never raise from load/save/enqueue: the capture path must stay safe
"""

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    DOMAIN,
    LIGHT_ACTION_BUFFER_MAX_ATTEMPTS,
    LIGHT_ACTION_BUFFER_MAX_ENTRIES,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.light_actions"


class LightActionBuffer:
    """
    Bounded, persisted FIFO of light action payloads not yet acknowledged.

    Storage location: .storage/linus_brain.light_actions
    Delivery is at-least-once: if the cloud stored a row but the response was
    lost, the replay inserts it again. Server-side deduplication is not
    guaranteed (light_actions.context_id has no unique constraint), so
    duplicates are possible after such an outage.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """
        Initialize the buffer.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._queue: deque[dict[str, Any]] = deque()
        self._loaded: bool = False
        self._replaying: bool = False

    async def async_load(self) -> None:
        """
        Load persisted entries from storage (idempotent, never raises).

        A missing or corrupted store yields an empty queue.
        """
        async with self._lock:
            await self._async_load_locked()

    async def _async_load_locked(self) -> None:
        """Load entries from storage; the caller must hold the lock."""
        if self._loaded:
            return

        try:
            data = await self._store.async_load()
            entries = data.get("entries", []) if data else []
            self._queue = deque(
                entry
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
            )
            self._trim()
            if self._queue:
                _LOGGER.info(f"Loaded {len(self._queue)} pending light actions")
        except Exception as err:
            _LOGGER.error(f"Failed to load pending light actions: {err}")
            self._queue = deque()

        self._loaded = True

    async def async_save(self) -> bool:
        """
        Persist the current queue.

        Returns:
            True if the queue was persisted, False otherwise
        """
        try:
            await self._store.async_save(
                {"version": STORAGE_VERSION, "entries": list(self._queue)}
            )
            return True
        except Exception as err:
            _LOGGER.error(f"Failed to save pending light actions: {err}")
            return False

    def get_pending_count(self) -> int:
        """
        Return the number of entries awaiting replay.

        Synchronous and side-effect free.
        """
        return len(self._queue)

    def _trim(self) -> None:
        """Evict the oldest entries beyond the queue bound."""
        while len(self._queue) > LIGHT_ACTION_BUFFER_MAX_ENTRIES:
            self._queue.popleft()

    async def async_enqueue(self, payload: dict[str, Any]) -> bool:
        """
        Buffer a payload that the cloud did not acknowledge.

        Args:
            payload: Light action payload as sent to the cloud

        Returns:
            True if the entry was persisted, False otherwise (never raises)
        """
        try:
            async with self._lock:
                await self._async_load_locked()
                self._queue.append(
                    {
                        "id": payload.get("context_id") or uuid.uuid4().hex,
                        "payload": payload,
                        "attempts": 0,
                        "queued_at": dt_util.utcnow().isoformat(),
                    }
                )
                self._trim()
                saved = await self.async_save()
            _LOGGER.debug(f"Buffered light action ({len(self._queue)} pending)")
            return saved
        except Exception as err:
            _LOGGER.error(f"Failed to buffer light action: {err}")
            return False

    async def async_replay_pending(
        self, send: Callable[[dict[str, Any]], Awaitable[bool]]
    ) -> int:
        """
        Replay pending entries through `send`.

        An entry is removed only once `send` returns True. A refused entry
        does not block the following ones; after too many attempts it is
        discarded. A concurrent call returns 0 immediately.

        Args:
            send: Coroutine function returning True when the cloud acknowledged

        Returns:
            Number of entries acknowledged during this replay
        """
        if self._replaying:
            return 0
        self._replaying = True

        acknowledged = 0
        try:
            async with self._lock:
                await self._async_load_locked()
                snapshot = list(self._queue)

            for entry in snapshot:
                try:
                    ok = bool(await send(entry["payload"]))
                except Exception as err:
                    _LOGGER.debug(f"Replay of buffered light action failed: {err}")
                    ok = False

                async with self._lock:
                    if ok:
                        self._remove(entry)
                        acknowledged += 1
                        continue

                    entry["attempts"] = entry.get("attempts", 0) + 1
                    if entry["attempts"] >= LIGHT_ACTION_BUFFER_MAX_ATTEMPTS:
                        self._remove(entry)
                        _LOGGER.warning(
                            f"Discarding light action {entry.get('id')} after "
                            f"{entry['attempts']} failed attempts"
                        )

            async with self._lock:
                await self.async_save()

            if acknowledged:
                _LOGGER.info(f"Replayed {acknowledged} buffered light actions")
            return acknowledged
        finally:
            self._replaying = False

    def _remove(self, entry: dict[str, Any]) -> None:
        """Remove an entry by identity (no-op if already gone)."""
        for index, item in enumerate(self._queue):
            if item is entry:
                del self._queue[index]
                return
