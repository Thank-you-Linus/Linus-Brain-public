"""
Test feature switch state restoration after Home Assistant restart.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from homeassistant.core import HomeAssistant, State
from homeassistant.config_entries import ConfigEntry

from ..const import DOMAIN
from ..switch import LinusBrainFeatureSwitch


@pytest.mark.asyncio
async def test_feature_switch_restores_on_state(hass: HomeAssistant) -> None:
    """Test that feature switch restores to ON state after restart."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Mock previous state (ON)
    previous_state = State("switch.linus_brain_feature_automatic_lighting_living_room", "on")

    # Create feature switch
    feature_def = {
        "name": "Automatic Lighting",
        "default_enabled": False
    }
    switch = LinusBrainFeatureSwitch(hass, entry, "living_room", "automatic_lighting", feature_def)

    # Mock coordinator with feature flag manager
    coordinator = MagicMock()
    coordinator.feature_flag_manager = MagicMock()
    coordinator.feature_flag_manager.is_feature_enabled.return_value = True
    hass.data[DOMAIN] = {
        "test_entry": {
            "coordinator": coordinator,
        }
    }

    # Mock async_get_last_state to return previous ON state
    with patch.object(switch, "async_get_last_state", return_value=previous_state):
        await switch.async_added_to_hass()

    # Verify state was restored to ON
    assert switch.is_on is True

    # Verify feature flag manager was checked
    coordinator.feature_flag_manager.is_feature_enabled.assert_called_once_with("living_room", "automatic_lighting")


@pytest.mark.asyncio
async def test_feature_switch_restores_off_state(hass: HomeAssistant) -> None:
    """Test that feature switch restores to OFF state after restart."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Mock previous state (OFF)
    previous_state = State("switch.linus_brain_feature_automatic_lighting_living_room", "off")

    # Create feature switch
    feature_def = {
        "name": "Automatic Lighting",
        "default_enabled": False
    }
    switch = LinusBrainFeatureSwitch(hass, entry, "living_room", "automatic_lighting", feature_def)

    # Mock coordinator with feature flag manager
    coordinator = MagicMock()
    coordinator.feature_flag_manager = MagicMock()
    coordinator.feature_flag_manager.is_feature_enabled.return_value = False
    hass.data[DOMAIN] = {
        "test_entry": {
            "coordinator": coordinator,
        }
    }

    # Mock async_get_last_state to return previous OFF state
    with patch.object(switch, "async_get_last_state", return_value=previous_state):
        await switch.async_added_to_hass()

    # Verify state was restored to OFF
    assert switch.is_on is False

    # Verify feature flag manager was checked
    coordinator.feature_flag_manager.is_feature_enabled.assert_called_once_with("living_room", "automatic_lighting")


@pytest.mark.asyncio
async def test_feature_switch_defaults_to_off_when_no_previous_state(
    hass: HomeAssistant,
) -> None:
    """Test that feature switch defaults to OFF when no previous state exists."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Create feature switch
    feature_def = {
        "name": "Automatic Lighting",
        "default_enabled": False
    }
    switch = LinusBrainFeatureSwitch(hass, entry, "living_room", "automatic_lighting", feature_def)

    # Mock coordinator with feature flag manager
    coordinator = MagicMock()
    coordinator.feature_flag_manager = MagicMock()
    coordinator.feature_flag_manager.is_feature_enabled.return_value = False  # Default OFF
    hass.data[DOMAIN] = {
        "test_entry": {
            "coordinator": coordinator,
        }
    }

    # Mock async_get_last_state to return None (no previous state)
    with patch.object(switch, "async_get_last_state", return_value=None):
        await switch.async_added_to_hass()

    # Verify state defaults to OFF
    assert switch.is_on is False

    # Verify feature flag manager was checked
    coordinator.feature_flag_manager.is_feature_enabled.assert_called_once_with("living_room", "automatic_lighting")


@pytest.mark.asyncio
async def test_feature_switch_turn_on_updates_feature_flag(hass: HomeAssistant) -> None:
    """Test that turning switch ON updates the feature flag."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Create feature switch
    feature_def = {
        "name": "Automatic Lighting",
        "default_enabled": False
    }
    switch = LinusBrainFeatureSwitch(hass, entry, "living_room", "automatic_lighting", feature_def)

    # Mock coordinator with feature flag manager
    coordinator = MagicMock()
    coordinator.feature_flag_manager = MagicMock()
    coordinator.feature_flag_manager.is_feature_enabled.return_value = False
    coordinator.feature_flag_manager.set_feature_enabled = MagicMock()
    hass.data[DOMAIN] = {
        "test_entry": {
            "coordinator": coordinator,
        }
    }

    # Mock async_get_last_state to return None (no previous state)
    with patch.object(switch, "async_get_last_state", return_value=None):
        await switch.async_added_to_hass()

    # Mock async_write_ha_state to avoid HA platform requirements
    with patch.object(switch, "async_write_ha_state"):
        # Turn switch ON
        await switch.async_turn_on()

    # Verify feature flag was updated
    coordinator.feature_flag_manager.set_feature_enabled.assert_called_once_with("living_room", "automatic_lighting", True)
    
    # Verify switch state is ON
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_feature_switch_turn_off_updates_feature_flag(hass: HomeAssistant) -> None:
    """Test that turning switch OFF updates the feature flag."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Create feature switch
    feature_def = {
        "name": "Automatic Lighting",
        "default_enabled": False
    }
    switch = LinusBrainFeatureSwitch(hass, entry, "living_room", "automatic_lighting", feature_def)

    # Mock coordinator with feature flag manager
    coordinator = MagicMock()
    coordinator.feature_flag_manager = MagicMock()
    coordinator.feature_flag_manager.is_feature_enabled.return_value = True
    coordinator.feature_flag_manager.set_feature_enabled = MagicMock()
    hass.data[DOMAIN] = {
        "test_entry": {
            "coordinator": coordinator,
        }
    }

    # Mock async_get_last_state to return None (no previous state)
    with patch.object(switch, "async_get_last_state", return_value=None):
        await switch.async_added_to_hass()

    # Mock async_write_ha_state to avoid HA platform requirements
    with patch.object(switch, "async_write_ha_state"):
        # Turn switch OFF
        await switch.async_turn_off()

    # Verify feature flag was updated
    coordinator.feature_flag_manager.set_feature_enabled.assert_called_once_with("living_room", "automatic_lighting", False)
    
    # Verify switch state is OFF
    assert switch.is_on is False