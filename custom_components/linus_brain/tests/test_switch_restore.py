"""
Test switch state restoration after Home Assistant restart.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from homeassistant.core import HomeAssistant, State
from homeassistant.config_entries import ConfigEntry

from ..const import DOMAIN
from ..switch import LinusAutoLightSwitch


@pytest.mark.asyncio
async def test_switch_restores_on_state(hass: HomeAssistant) -> None:
    """Test that switch restores to ON state after restart."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Mock previous state (ON)
    previous_state = State("switch.linus_brain_autolight_salon", "on")

    # Create switch
    switch = LinusAutoLightSwitch(hass, entry, "salon", "Salon")

    # Mock rule engine
    rule_engine = AsyncMock()
    hass.data[DOMAIN] = {
        "test_entry": {
            "rule_engine": rule_engine,
        }
    }

    # Mock async_get_last_state to return previous ON state
    with patch.object(switch, "async_get_last_state", return_value=previous_state):
        await switch.async_added_to_hass()

    # Verify state was restored to ON
    assert switch.is_on is True

    # Verify rule engine was enabled for the area
    rule_engine.enable_area.assert_called_once_with("salon")
    rule_engine.disable_area.assert_not_called()


@pytest.mark.asyncio
async def test_switch_restores_off_state(hass: HomeAssistant) -> None:
    """Test that switch restores to OFF state after restart."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Mock previous state (OFF)
    previous_state = State("switch.linus_brain_autolight_salon", "off")

    # Create switch
    switch = LinusAutoLightSwitch(hass, entry, "salon", "Salon")

    # Mock rule engine
    rule_engine = AsyncMock()
    hass.data[DOMAIN] = {
        "test_entry": {
            "rule_engine": rule_engine,
        }
    }

    # Mock async_get_last_state to return previous OFF state
    with patch.object(switch, "async_get_last_state", return_value=previous_state):
        await switch.async_added_to_hass()

    # Verify state was restored to OFF
    assert switch.is_on is False

    # Verify rule engine was disabled for the area
    rule_engine.disable_area.assert_called_once_with("salon")
    rule_engine.enable_area.assert_not_called()


@pytest.mark.asyncio
async def test_switch_defaults_to_off_when_no_previous_state(
    hass: HomeAssistant,
) -> None:
    """Test that switch defaults to OFF when no previous state exists."""
    # Mock config entry
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry"

    # Create switch
    switch = LinusAutoLightSwitch(hass, entry, "salon", "Salon")

    # Mock rule engine
    rule_engine = AsyncMock()
    hass.data[DOMAIN] = {
        "test_entry": {
            "rule_engine": rule_engine,
        }
    }

    # Mock async_get_last_state to return None (no previous state)
    with patch.object(switch, "async_get_last_state", return_value=None):
        await switch.async_added_to_hass()

    # Verify state defaults to OFF
    assert switch.is_on is False

    # Verify rule engine was disabled for the area
    rule_engine.disable_area.assert_called_once_with("salon")
    rule_engine.enable_area.assert_not_called()
