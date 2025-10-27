"""
Button Platform for Linus Brain

This module provides button entities for triggering actions in the integration.
"""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LinusBrainCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up Linus Brain button entities from a config entry.

    Args:
        hass: Home Assistant instance
        entry: Config entry
        async_add_entities: Callback to add entities
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    buttons = [
        LinusBrainSyncButton(coordinator, entry),
    ]

    async_add_entities(buttons)
    _LOGGER.info(f"Added {len(buttons)} Linus Brain button entities")


class LinusBrainSyncButton(CoordinatorEntity, ButtonEntity):
    """
    Button to trigger immediate cloud sync.
    """

    def __init__(self, coordinator: LinusBrainCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_name = "Sync Now"
        self._attr_translation_key = "sync"
        self._attr_unique_id = "sync"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:cloud-sync"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Linus Brain",
            "manufacturer": "Linus Brain",
            "model": "Automation Engine",
        }

    async def async_press(self) -> None:
        """Handle the button press - full sync including apps and activities."""
        _LOGGER.info("Full cloud sync button pressed")

        coordinator: LinusBrainCoordinator = self.coordinator  # type: ignore

        if coordinator.instance_id:
            app_storage = coordinator.app_storage
            area_ids = list(coordinator.area_manager.get_all_areas().keys())

            _LOGGER.info("Reloading apps and activities from cloud...")
            await app_storage.async_sync_from_cloud(
                coordinator.supabase_client, coordinator.instance_id, area_ids
            )

            await coordinator.activity_tracker.async_initialize(force_reload=True)
            _LOGGER.info("Apps and activities reloaded")

        _LOGGER.info("Syncing area states...")
        await self.coordinator.async_refresh()
        _LOGGER.info("Full cloud sync completed")
