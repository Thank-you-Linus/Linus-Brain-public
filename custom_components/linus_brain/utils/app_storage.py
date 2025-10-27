"""
App Storage Manager for Linus Brain

3-tier offline-first storage architecture with cloud-first sync:

STORAGE LAYERS:
1. Hardcoded fallback (const.py) - Ultimate fallback for first install
2. Local cache (.storage JSON) - Fast offline access
3. Cloud sync (Supabase) - Source of truth for configuration

CLOUD-FIRST SYNC PHILOSOPHY:
- Cloud is the source of truth for all configuration
- Empty cloud data is VALID (intentional no-assignment state)
- Sync always updates local with cloud state (even if empty)
- Only use fallback when cloud AND local are both empty
- On error/timeout: preserve existing local data (graceful degradation)

SYNC SCENARIOS:
1. Cloud has data → Download and save locally
2. Cloud empty, local has data → Clear local (cloud wins)
3. Cloud empty, local empty → Load fallback
4. Cloud timeout/error → Keep existing local data
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "linus_brain.apps"
CLOUD_SYNC_TIMEOUT = 10


class AppStorage:
    """
    Manages 3-tier storage for activities, apps, and assignments.

    Storage priority:
    1. Try load from local cache (.storage/linus_brain.apps)
    2. Try sync from cloud (timeout 10s)
    3. If cloud fails/empty AND no local data → load hardcoded fallback
    4. Save to local cache for next load

    All operations are non-blocking and gracefully degrade.
    """

    def __init__(self, hass: HomeAssistant, storage_dir: Path | None = None) -> None:
        """
        Initialize app storage manager.

        Args:
            hass: Home Assistant instance
            storage_dir: Optional custom storage directory (for testing)
        """
        self.hass = hass

        if storage_dir:
            self.storage_dir = storage_dir
        else:
            self.storage_dir = Path(hass.config.path(".storage"))

        self.storage_file = self.storage_dir / f"{STORAGE_KEY}"

        self._data: dict[str, Any] = {
            "version": STORAGE_VERSION,
            "activities": {},
            "apps": {},
            "assignments": {},
            "synced_at": None,
            "is_fallback": False,
        }

    def is_empty(self) -> bool:
        """
        Check if storage is empty (no activities, apps, or assignments).

        Returns:
            True if completely empty
        """
        return (
            not self._data.get("activities")
            and not self._data.get("apps")
            and not self._data.get("assignments")
        )

    async def async_load(self) -> dict[str, Any]:
        """
        Load data from local cache.

        Returns:
            Loaded data dictionary
        """
        try:
            if not self.storage_file.exists():
                _LOGGER.debug("No local storage file found")
                return self._data

            with open(self.storage_file, "r") as f:
                loaded_data = json.load(f)

            if loaded_data.get("version") != STORAGE_VERSION:
                _LOGGER.warning(
                    f"Storage version mismatch: {loaded_data.get('version')} != {STORAGE_VERSION}"
                )
                return self._data

            self._data = loaded_data

            _LOGGER.info(
                f"Loaded from cache: {len(self._data.get('activities', {}))} activities, "
                f"{len(self._data.get('apps', {}))} apps, "
                f"{len(self._data.get('assignments', {}))} assignments"
            )

            return self._data

        except Exception as err:
            _LOGGER.error(f"Failed to load from cache: {err}")
            return self._data

    async def async_save(self) -> bool:
        """
        Save current data to local cache.

        Returns:
            True if successful
        """
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)

            with open(self.storage_file, "w") as f:
                json.dump(self._data, f, indent=2, default=str)

            _LOGGER.debug(f"Saved to cache: {self.storage_file}")
            return True

        except Exception as err:
            _LOGGER.error(f"Failed to save to cache: {err}")
            return False

    def load_hardcoded_fallback(self) -> dict[str, Any]:
        """
        Load hardcoded fallback data from const.py.

        This is the ultimate fallback when:
        - No cloud connection AND no local data
        - First installation

        Returns:
            Fallback data dictionary
        """
        from ..const import DEFAULT_ACTIVITY_TYPES, DEFAULT_AUTOLIGHT_APP

        self._data = {
            "version": STORAGE_VERSION,
            "activities": DEFAULT_ACTIVITY_TYPES,
            "apps": {"autolight": DEFAULT_AUTOLIGHT_APP},
            "assignments": {},
            "synced_at": None,
            "is_fallback": True,
        }

        _LOGGER.warning(
            "Using hardcoded fallback: 3 activities, 1 app (autolight), 0 assignments"
        )

        return self._data

    async def async_sync_from_cloud(
        self, supabase_client, instance_id: str, area_ids: list[str]
    ) -> bool:
        """
        Sync data from cloud (Supabase).

        CLOUD-FIRST STRATEGY (Cloud is source of truth):
        1. Try fetch activities, apps, assignments from cloud (timeout 10s)
        2. If cloud succeeds (even if empty) → update local cache with cloud data
        3. If cloud returns empty assignments → clear local assignments (intentional)
        4. Only load fallback if: cloud returns empty AND local becomes completely empty
        5. If cloud fails/timeout → keep existing local data (graceful degradation)

        CLIENT-SPECIFIC LOADING:
        - Only download apps assigned to THIS client's areas
        - Don't download entire app catalog
        - Fetch activities referenced by those apps only

        Args:
            supabase_client: SupabaseClient instance
            instance_id: HA instance UUID
            area_ids: List of area IDs for this client

        Returns:
            True if sync succeeded, False otherwise
        """
        try:
            _LOGGER.info("Attempting cloud sync (timeout 10s)")

            async with asyncio.timeout(CLOUD_SYNC_TIMEOUT):
                assignments = await supabase_client.fetch_area_assignments(instance_id)

                apps = {}
                activity_ids = set()

                if assignments:
                    assigned_app_ids = set()
                    for assignment in assignments.values():
                        assigned_app_ids.add(assignment["app_id"])

                    for app_id in assigned_app_ids:
                        app_data = await supabase_client.fetch_app_with_actions(
                            app_id, version=None
                        )

                        if app_data:
                            apps[app_id] = app_data

                            for activity_id in app_data.get(
                                "activity_actions", {}
                            ).keys():
                                activity_ids.add(activity_id)

                    activities = await supabase_client.fetch_activity_types(
                        list(activity_ids)
                    )
                else:
                    _LOGGER.info("No assignments in cloud (empty is valid state)")
                    activities = {}

                self._data = {
                    "version": STORAGE_VERSION,
                    "activities": activities,
                    "apps": apps,
                    "assignments": assignments,
                    "synced_at": dt_util.utcnow().isoformat(),
                    "is_fallback": False,
                }

                if self.is_empty():
                    _LOGGER.info("Cloud data empty → loading fallback")
                    self.load_hardcoded_fallback()

                await self.async_save()

                _LOGGER.info(
                    f"Cloud sync successful: {len(self._data.get('activities', {}))} activities, "
                    f"{len(self._data.get('apps', {}))} apps, "
                    f"{len(self._data.get('assignments', {}))} assignments"
                )

                return True

        except asyncio.TimeoutError:
            _LOGGER.warning("Cloud sync timeout (10s)")

            if self.is_empty():
                _LOGGER.info("No local data → using fallback")
                self.load_hardcoded_fallback()
                await self.async_save()

            return False

        except Exception as err:
            _LOGGER.warning(f"Cloud sync failed: {err}")

            if self.is_empty():
                _LOGGER.info("No local data → using fallback")
                self.load_hardcoded_fallback()
                await self.async_save()

            return False

    def get_activities(self) -> dict[str, Any]:
        """Get all activities."""
        return self._data.get("activities", {})

    def get_activity(self, activity_id: str) -> dict[str, Any] | None:
        """Get specific activity by ID."""
        return self._data.get("activities", {}).get(activity_id)

    def get_apps(self) -> dict[str, Any]:
        """Get all apps."""
        return self._data.get("apps", {})

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        """Get specific app by ID."""
        return self._data.get("apps", {}).get(app_id)

    def get_assignments(self) -> dict[str, Any]:
        """Get all area assignments."""
        return self._data.get("assignments", {})

    def get_assignment(self, area_id: str) -> dict[str, Any] | None:
        """Get assignment for specific area."""
        return self._data.get("assignments", {}).get(area_id)

    def set_activity(self, activity_id: str, activity_data: dict[str, Any]) -> None:
        """
        Set or update an activity.

        Args:
            activity_id: Activity identifier
            activity_data: Activity configuration
        """
        if "activities" not in self._data:
            self._data["activities"] = {}

        self._data["activities"][activity_id] = activity_data
        _LOGGER.debug(f"Updated activity: {activity_id}")

    def set_app(self, app_id: str, app_data: dict[str, Any]) -> None:
        """
        Set or update an app.

        Args:
            app_id: App identifier
            app_data: App configuration with activity_actions
        """
        if "apps" not in self._data:
            self._data["apps"] = {}

        self._data["apps"][app_id] = app_data
        _LOGGER.debug(f"Updated app: {app_id}")

    def set_assignment(self, area_id: str, assignment_data: dict[str, Any]) -> None:
        """
        Set or update an area assignment.

        Args:
            area_id: Area identifier
            assignment_data: Assignment configuration
        """
        if "assignments" not in self._data:
            self._data["assignments"] = {}

        self._data["assignments"][area_id] = assignment_data
        _LOGGER.debug(f"Updated assignment for area: {area_id}")

    def remove_assignment(self, area_id: str) -> bool:
        """
        Remove assignment for an area.

        Args:
            area_id: Area identifier

        Returns:
            True if removed, False if didn't exist
        """
        if area_id in self._data.get("assignments", {}):
            del self._data["assignments"][area_id]
            _LOGGER.debug(f"Removed assignment for area: {area_id}")
            return True

        return False

    def is_fallback_data(self) -> bool:
        """Check if currently using fallback data."""
        return self._data.get("is_fallback", False)

    def get_sync_time(self) -> datetime | None:
        """Get last cloud sync timestamp."""
        synced_at = self._data.get("synced_at")
        if synced_at:
            return dt_util.parse_datetime(synced_at)
        return None

    async def async_initialize(
        self, supabase_client, instance_id: str, area_ids: list[str]
    ) -> dict[str, Any]:
        """
        Initialize storage with full load sequence.

        LOAD SEQUENCE (cloud-first for ALL scenarios):
        1. Load from local cache
        2. Try sync from cloud (non-blocking, timeout 10s)
        3. If cloud succeeds → update cache
        4. If cloud fails/empty AND no local data → use fallback
        5. Return current data

        Args:
            supabase_client: SupabaseClient instance
            instance_id: HA instance UUID
            area_ids: List of area IDs for this client

        Returns:
            Loaded data dictionary
        """
        await self.async_load()

        await self.async_sync_from_cloud(supabase_client, instance_id, area_ids)

        if self.is_empty():
            _LOGGER.warning("After sync, still empty → loading fallback")
            self.load_hardcoded_fallback()
            await self.async_save()

        return self._data
