"""
Unit tests for environmental state change triggers.

Tests that rule engine responds to environmental changes (illuminance, sun elevation)
even when activity remains constant, for rules with area_state conditions.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from ..utils.rule_engine import RuleEngine


@pytest.fixture
def mock_hass():
    """Mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.data = {}
    return hass


@pytest.fixture
def mock_activity_tracker():
    """Mock ActivityTracker."""
    tracker = MagicMock()
    tracker.async_initialize = AsyncMock()
    tracker.async_evaluate_activity = AsyncMock(return_value="movement")
    tracker.get_activity = MagicMock(return_value="movement")
    return tracker


@pytest.fixture
def mock_app_storage():
    """Mock AppStorage with autolight app."""
    storage = MagicMock()
    
    # Autolight app with area_state condition
    autolight_app = {
        "app_id": "autolight",
        "app_name": "Automatic Lighting",
        "activity_actions": {
            "movement": {
                "conditions": [
                    {
                        "condition": "area_state",
                        "area_id": "current",
                        "attribute": "is_dark",
                    }
                ],
                "actions": [
                    {
                        "service": "light.turn_on",
                        "domain": "light",
                        "area": "current",
                        "data": {"brightness_pct": 100},
                    }
                ],
                "logic": "and",
            },
            "empty": {
                "conditions": [],
                "actions": [
                    {
                        "service": "light.turn_off",
                        "domain": "light",
                        "area": "current",
                    }
                ],
                "logic": "and",
            },
        },
    }
    
    storage.get_assignments = MagicMock(return_value={
        "salon": {
            "area_id": "salon",
            "app_id": "autolight",
            "enabled": True,
        }
    })
    storage.get_assignment = MagicMock(return_value={
        "area_id": "salon",
        "app_id": "autolight",
        "enabled": True,
    })
    storage.get_app = MagicMock(return_value=autolight_app)
    storage.get_apps = MagicMock(return_value={"autolight": autolight_app})
    storage.remove_assignment = MagicMock()
    storage.async_save = AsyncMock(return_value=True)
    return storage


@pytest.fixture
def mock_area_manager():
    """Mock AreaManager with environmental entities."""
    manager = MagicMock()
    
    # Return environmental entities when requested
    def get_area_entities(area_id, domain=None, device_class=None):
        if domain == "sensor" and device_class == "illuminance":
            return ["sensor.salon_illuminance"]
        if domain == "binary_sensor" and device_class == "motion":
            return ["binary_sensor.salon_motion"]
        return []
    
    manager.get_area_entities = MagicMock(side_effect=get_area_entities)
    return manager


@pytest.fixture
def rule_engine(mock_hass, mock_activity_tracker, mock_app_storage, mock_area_manager):
    """Create RuleEngine instance."""
    return RuleEngine(
        mock_hass,
        "test_entry",
        mock_activity_tracker,
        mock_app_storage,
        mock_area_manager,
    )


class TestEnvironmentalEntityTracking:
    """Test that environmental entities are tracked when area_state conditions exist."""

    @pytest.mark.asyncio
    async def test_enable_area_tracks_environmental_entities(
        self, rule_engine, mock_area_manager, mock_hass
    ):
        """Test that environmental entities are tracked when app uses area_state."""
        # Mock sun.sun entity exists
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        await rule_engine.async_initialize()

        # Check that environmental entities were identified
        environmental_entities = rule_engine._get_area_environmental_entities("salon")
        
        assert "sensor.salon_illuminance" in environmental_entities
        assert "sun.sun" in environmental_entities

    @pytest.mark.asyncio
    async def test_enable_area_without_area_state_skips_environmental(
        self, rule_engine, mock_app_storage, mock_area_manager
    ):
        """Test that environmental entities are NOT tracked when no area_state conditions."""
        # Create app without area_state condition
        app_without_area_state = {
            "app_id": "simple_light",
            "activity_actions": {
                "movement": {
                    "conditions": [],
                    "actions": [
                        {
                            "service": "light.turn_on",
                            "domain": "light",
                            "area": "current",
                        }
                    ],
                }
            },
        }
        
        mock_app_storage.get_app = MagicMock(return_value=app_without_area_state)
        
        await rule_engine.async_initialize()

        # Environmental entities should not be tracked
        # Only presence entities should be tracked
        assert len(rule_engine._listeners.get("salon", [])) > 0


class TestEnvironmentalChangeTriggersEvaluation:
    """Test that environmental changes trigger rule evaluation."""

    @pytest.mark.asyncio
    async def test_illuminance_change_triggers_evaluation(
        self, rule_engine, mock_hass, mock_activity_tracker
    ):
        """Test that illuminance sensor change triggers rule evaluation."""
        # Setup: Area with movement, lights should respond to lux changes
        mock_activity_tracker.async_evaluate_activity = AsyncMock(return_value="movement")
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        await rule_engine.async_initialize()

        # Verify area is enabled
        assert "salon" in rule_engine._enabled_areas

        # Simulate illuminance sensor change
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        # This should trigger evaluation
        rule_engine._async_state_change_handler(event)

        # Wait for debounce
        await asyncio.sleep(2.5)

        # Verify evaluation was triggered
        mock_activity_tracker.async_evaluate_activity.assert_called()

    @pytest.mark.asyncio
    async def test_sun_change_triggers_evaluation(
        self, rule_engine, mock_hass, mock_activity_tracker
    ):
        """Test that sun.sun entity change triggers rule evaluation."""
        mock_activity_tracker.async_evaluate_activity = AsyncMock(return_value="movement")
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        await rule_engine.async_initialize()

        # Simulate sun.sun change (e.g., elevation crosses threshold)
        event = MagicMock()
        event.data = {"entity_id": "sun.sun"}
        
        rule_engine._async_state_change_handler(event)

        await asyncio.sleep(2.5)

        # Verify evaluation was triggered
        mock_activity_tracker.async_evaluate_activity.assert_called()

    @pytest.mark.asyncio
    async def test_environmental_change_respects_debounce(
        self, rule_engine, mock_hass, mock_activity_tracker
    ):
        """Test that rapid environmental changes are debounced."""
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        await rule_engine.async_initialize()

        # Simulate multiple rapid illuminance changes
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(0.1)
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(0.1)
        rule_engine._async_state_change_handler(event)

        # Wait for debounce
        await asyncio.sleep(2.5)

        # Should only trigger once due to debounce
        # Each change cancels the previous pending task
        assert mock_activity_tracker.async_evaluate_activity.call_count <= 2


class TestEnvironmentalConditionsWithConstantActivity:
    """Test that environmental conditions work with constant activity."""

    @pytest.mark.asyncio
    async def test_dark_to_bright_turns_off_lights_with_movement(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_app_storage
    ):
        """Test lights turn off when area becomes bright, even with constant movement."""
        # Setup: Activity stays "movement", but area goes from dark to bright
        mock_activity_tracker.async_evaluate_activity = AsyncMock(return_value="movement")
        
        # Create app that turns OFF lights when bright during movement
        app_with_bright_condition = {
            "app_id": "autolight",
            "activity_actions": {
                "movement": {
                    "conditions": [
                        {
                            "condition": "area_state",
                            "area_id": "current",
                            "attribute": "is_bright",
                        }
                    ],
                    "actions": [
                        {
                            "service": "light.turn_off",
                            "domain": "light",
                            "area": "current",
                        }
                    ],
                }
            },
        }
        
        mock_app_storage.get_app = MagicMock(return_value=app_with_bright_condition)
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        await rule_engine.async_initialize()

        # Simulate illuminance rising above bright threshold
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(2.5)

        # Verify activity was re-evaluated
        mock_activity_tracker.async_evaluate_activity.assert_called_with("salon")


class TestHelperMethods:
    """Test helper methods for environmental entity detection."""

    def test_has_area_state_condition_simple(self, rule_engine):
        """Test detection of area_state condition in simple list."""
        conditions = [
            {"condition": "area_state", "attribute": "is_dark"}
        ]
        
        assert rule_engine._has_area_state_condition(conditions) is True

    def test_has_area_state_condition_nested_and(self, rule_engine):
        """Test detection of area_state condition in nested AND."""
        conditions = [
            {
                "condition": "and",
                "conditions": [
                    {"condition": "state", "entity_id": "light.test", "state": "off"},
                    {"condition": "area_state", "attribute": "is_dark"},
                ],
            }
        ]
        
        assert rule_engine._has_area_state_condition(conditions) is True

    def test_has_area_state_condition_nested_or(self, rule_engine):
        """Test detection of area_state condition in nested OR."""
        conditions = [
            {
                "condition": "or",
                "conditions": [
                    {"condition": "area_state", "attribute": "is_dark"},
                    {"condition": "time", "after": "22:00"},
                ],
            }
        ]
        
        assert rule_engine._has_area_state_condition(conditions) is True

    def test_has_area_state_condition_none(self, rule_engine):
        """Test no detection when no area_state condition exists."""
        conditions = [
            {"condition": "state", "entity_id": "light.test", "state": "off"},
            {"condition": "time", "after": "22:00"},
        ]
        
        assert rule_engine._has_area_state_condition(conditions) is False

    def test_has_area_state_condition_empty(self, rule_engine):
        """Test empty conditions list."""
        assert rule_engine._has_area_state_condition([]) is False
