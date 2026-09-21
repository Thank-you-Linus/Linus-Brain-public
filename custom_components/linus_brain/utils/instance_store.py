"""
Local storage for the cloud instance identity.

Persists the instance identity (`instance_id` + `ha_installation_id`) in
.storage/linus_brain.instance so a Home Assistant restart does not require a
cloud round-trip to recover it.

Key responsibilities:
- Persist the identity through Home Assistant's Store helper (atomic writes)
- Survive a corrupted or missing store by returning an empty identity
- Never raise: the setup path must stay loadable when the cloud is unreachable
"""

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.instance"


class InstanceStore:
    """
    Manages persistent local storage for the cloud instance identity.

    Uses Home Assistant's Store helper for atomic writes and corruption handling.
    Storage location: .storage/linus_brain.instance
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """
        Initialize instance storage.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._identity: dict[str, Any] = {}

    async def async_load(self) -> dict[str, Any]:
        """
        Load the persisted identity from storage.

        Returns:
            Dictionary with "instance_id" and "ha_installation_id", or {} if
            nothing is persisted or the store could not be read
        """
        async with self._lock:
            try:
                data = await self._store.async_load()

                if data is None:
                    _LOGGER.info("No persisted instance identity found in storage")
                    self._identity = {}
                    return self._identity

                instance_id = data.get("instance_id")
                if not instance_id:
                    _LOGGER.info("Persisted instance identity has no instance_id")
                    self._identity = {}
                    return self._identity

                self._identity = {
                    "instance_id": instance_id,
                    "ha_installation_id": data.get("ha_installation_id"),
                }
                _LOGGER.info(f"Loaded persisted instance identity: {instance_id}")

                return self._identity

            except Exception as err:
                _LOGGER.error(f"Failed to load instance identity from storage: {err}")
                self._identity = {}
                return self._identity

    async def async_save(
        self, instance_id: str, ha_installation_id: str | None
    ) -> bool:
        """
        Persist the instance identity.

        Args:
            instance_id: Cloud instance UUID
            ha_installation_id: HA installation fingerprint (core.uuid)

        Returns:
            True if the identity was persisted, False otherwise
        """
        async with self._lock:
            if not instance_id:
                _LOGGER.warning("Refusing to persist an empty instance_id")
                return False

            try:
                data = {
                    "version": STORAGE_VERSION,
                    "instance_id": instance_id,
                    "ha_installation_id": ha_installation_id,
                }

                await self._store.async_save(data)
                self._identity = {
                    "instance_id": instance_id,
                    "ha_installation_id": ha_installation_id,
                }
                _LOGGER.debug(f"Saved instance identity to storage: {instance_id}")

                return True

            except Exception as err:
                _LOGGER.error(f"Failed to save instance identity to storage: {err}")
                return False
