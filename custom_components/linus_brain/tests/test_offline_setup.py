"""
Integration tests for a setup running against an unavailable database.

Covers the degraded setup path introduced with the persisted instance identity:
- A previously persisted identity is restored without any cloud lookup
- Setup completes and loads platforms when the cloud is down and no identity
  is persisted (instance_id stays None)
- Downstream consumers tolerate an unknown instance_id
- An identity obtained once the cloud returns is persisted for next restart
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import PLATFORMS, async_setup_entry
from .. import coordinator as coordinator_module
from ..const import CONF_SUPABASE_KEY, CONF_SUPABASE_URL, DOMAIN
from ..coordinator import LinusBrainCoordinator
from ..utils.app_storage import AppStorage
from ..utils.insights_manager import InsightsManager

HA_UUID = "ha-installation-uuid-0001"
INSTANCE_ID = "cloud-instance-uuid-0001"


def _make_supabase_client(*, available: bool) -> AsyncMock:
    """
    Build a Supabase client mock.

    Args:
        available: True for a reachable database returning a known instance,
            False for a database that never answers (None everywhere, which is
            how SupabaseClient signals "unreachable")

    Returns:
        An AsyncMock usable as coordinator.supabase_client
    """
    client = AsyncMock()

    if available:
        client.get_instance_by_ha_id.return_value = {"instance_id": INSTANCE_ID}
    else:
        client.get_instance_by_ha_id.return_value = None

    client.create_new_instance.return_value = None
    client.update_instance_last_seen.return_value = None
    client.fetch_area_insights.return_value = None
    client.fetch_activity_types.return_value = None
    client.fetch_app_with_actions.return_value = None
    return client


@pytest.fixture
def config_entry(hass):
    """Create and register a config entry for the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SUPABASE_URL: "https://example.supabase.co",
            CONF_SUPABASE_KEY: "fake-key",
        },
        options={},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def seeded_hass(hass):
    """Seed the HA installation fingerprint used as instance fingerprint."""
    hass.data["core.uuid"] = HA_UUID
    return hass


async def _run_setup(hass, entry, client):
    """
    Run async_setup_entry with a given Supabase client mock.

    The entry is put in SETUP_IN_PROGRESS, the state Home Assistant guarantees
    when it calls async_setup_entry itself: async_config_entry_first_refresh()
    runs for real, it is never mocked away.
    """
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    with patch.object(coordinator_module, "SupabaseClient", return_value=client):
        return await async_setup_entry(hass, entry)


class TestOfflineSetup:
    """Setup behaviour when the database is unavailable."""

    async def test_setup_succeeds_without_identity_when_cloud_down(
        self, seeded_hass, config_entry, enable_custom_integrations
    ):
        """
        Criterion 2: no identity persisted and no cloud → setup still completes,
        platforms are forwarded, instance_id stays None, nothing propagates.
        """
        client = _make_supabase_client(available=False)

        with patch.object(
            seeded_hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ) as forward:
            result = await _run_setup(seeded_hass, config_entry, client)

        assert result is True
        forward.assert_awaited_once_with(config_entry, PLATFORMS)

        coordinator = seeded_hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
        assert coordinator.instance_id is None

    async def test_persisted_identity_survives_a_cloud_outage(
        self, seeded_hass, config_entry, enable_custom_integrations
    ):
        """
        Criterion 1: after a successful setup, a second setup against a broken
        database returns the same instance_id without any cloud lookup.
        """
        online_client = _make_supabase_client(available=True)

        with patch.object(
            seeded_hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ):
            assert await _run_setup(seeded_hass, config_entry, online_client) is True

        coordinator = seeded_hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
        assert coordinator.instance_id == INSTANCE_ID

        # Second setup: the database is now systematically unavailable
        offline_client = _make_supabase_client(available=False)

        with patch.object(
            seeded_hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ):
            assert await _run_setup(seeded_hass, config_entry, offline_client) is True

        coordinator = seeded_hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
        assert coordinator.instance_id == INSTANCE_ID

        # The identity came from disk, not from the cloud
        offline_client.get_instance_by_ha_id.assert_not_awaited()
        offline_client.create_new_instance.assert_not_awaited()

    async def test_identity_obtained_later_is_persisted(
        self, seeded_hass, config_entry, enable_custom_integrations
    ):
        """
        Criterion 5: database down at setup, then back up. A fresh coordinator
        built on the same storage recovers the identity with no cloud lookup.
        """
        offline_client = _make_supabase_client(available=False)

        with patch.object(
            seeded_hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ):
            assert await _run_setup(seeded_hass, config_entry, offline_client) is True

        coordinator = seeded_hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
        assert coordinator.instance_id is None

        # Database comes back: the identity is fetched and persisted
        coordinator.supabase_client = _make_supabase_client(available=True)
        coordinator._identity_loaded = False
        assert await coordinator.get_or_create_instance_id() == INSTANCE_ID

        # A brand new coordinator (simulating a restart) finds it on disk
        restarted = LinusBrainCoordinator(
            hass=seeded_hass,
            supabase_url="https://example.supabase.co",
            supabase_key="fake-key",
            config_entry=config_entry,
        )
        restarted.supabase_client = _make_supabase_client(available=False)

        assert await restarted.get_or_create_instance_id() == INSTANCE_ID
        restarted.supabase_client.get_instance_by_ha_id.assert_not_awaited()
        restarted.supabase_client.create_new_instance.assert_not_awaited()


class TestIdentityFingerprint:
    """The persisted identity is scoped to its own HA installation."""

    async def test_identity_of_another_installation_is_ignored(
        self, seeded_hass, config_entry
    ):
        """A backup restored elsewhere must not claim another home's identity."""
        coordinator = LinusBrainCoordinator(
            hass=seeded_hass,
            supabase_url="https://example.supabase.co",
            supabase_key="fake-key",
            config_entry=config_entry,
        )
        coordinator.supabase_client = _make_supabase_client(available=False)
        await coordinator._instance_store.async_save(INSTANCE_ID, "some-other-home")

        with pytest.raises(Exception, match="Supabase unavailable"):
            await coordinator.get_or_create_instance_id()

        assert coordinator.instance_id is None

    async def test_known_identity_survives_a_missing_core_uuid(
        self, hass, config_entry
    ):
        """
        core.uuid absent: the persisted fingerprint is used instead of raising,
        and the known identity is returned without any cloud lookup.
        """
        hass.data.pop("core.uuid", None)

        coordinator = LinusBrainCoordinator(
            hass=hass,
            supabase_url="https://example.supabase.co",
            supabase_key="fake-key",
            config_entry=config_entry,
        )
        coordinator.supabase_client = _make_supabase_client(available=False)
        await coordinator._instance_store.async_save(INSTANCE_ID, HA_UUID)

        assert await coordinator.get_or_create_instance_id() == INSTANCE_ID
        coordinator.supabase_client.get_instance_by_ha_id.assert_not_awaited()
        coordinator.supabase_client.create_new_instance.assert_not_awaited()


class TestUnknownInstanceConsumers:
    """Downstream consumers must tolerate an unknown instance_id."""

    async def test_app_storage_and_insights_tolerate_unknown_instance(
        self, hass, tmp_path
    ):
        """
        Criterion 3: with instance_id None, app_storage initializes from the
        local cache / const.py fallback and insights_manager declines cleanly.
        """
        client = _make_supabase_client(available=False)

        app_storage = AppStorage(hass, storage_dir=tmp_path)
        await app_storage.async_initialize(client, None, ["salon"])

        assert app_storage.get_activities()

        insights_manager = InsightsManager(client)
        assert await insights_manager.async_load(None) is False

    async def test_insights_load_with_none_does_not_touch_cloud(self, hass):
        """The guard runs before any fetch, preserving the existing cache."""
        client = _make_supabase_client(available=False)
        insights_manager = InsightsManager(client)
        insights_manager._cache[("i", "salon", "lux_threshold")] = {"value": 42}

        assert await insights_manager.async_load(None) is False

        client.fetch_area_insights.assert_not_awaited()
        assert insights_manager._cache[("i", "salon", "lux_threshold")] == {"value": 42}
