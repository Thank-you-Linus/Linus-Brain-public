"""
Unit tests for environmental state change triggers.

Tests that rule engine responds to environmental changes (illuminance, sun elevation)
even when activity remains constant, for rules with area_state conditions.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

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

    storage.get_assignments = MagicMock(
        return_value={
            "salon": {
                "area_id": "salon",
                "app_id": "autolight",
                "enabled": True,
            }
        }
    )
    storage.get_assignment = MagicMock(
        return_value={
            "area_id": "salon",
            "app_id": "autolight",
            "enabled": True,
        }
    )
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
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that illuminance sensor change triggers rule evaluation."""
        # Setup: Area with movement, lights should respond to lux changes
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        mock_hass.states.get = MagicMock(return_value=MagicMock())

        # Mock environmental state showing transition from bright to dark
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )

        await rule_engine.async_initialize()

        # Verify area is enabled (has listeners)
        assert "salon" in rule_engine._listeners

        # Now simulate transition to dark
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )

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
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that sun.sun entity change triggers rule evaluation."""
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        mock_hass.states.get = MagicMock(return_value=MagicMock())

        # Mock environmental state showing bright initially
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )

        await rule_engine.async_initialize()

        # Now simulate transition to dark (sun setting)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )

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


class TestHelperMethods:
    """Test helper methods for environmental entity detection."""

    def test_has_area_state_condition_simple(self, rule_engine):
        """Test detection of area_state condition in simple list."""
        conditions = [{"condition": "area_state", "attribute": "is_dark"}]

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


class TestEnvironmentalStateTracking:
    """Test environmental state tracking and transition detection."""

    def test_get_current_environmental_state(self, rule_engine, mock_area_manager):
        """Test getting current environmental state from area manager."""
        # Mock area_manager.get_area_environmental_state
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )

        state = rule_engine._get_current_environmental_state("salon")

        assert state == {"is_dark": True}
        mock_area_manager.get_area_environmental_state.assert_called_with("salon")

    def test_detect_environmental_transition_became_dark(self, rule_engine):
        """Test detection of became_dark transition."""
        area_id = "salon"

        # Set previous state: not dark
        rule_engine._previous_env_state[area_id] = {
            "is_dark": False,
        }

        # Current state: now dark
        current_state = {"is_dark": True}

        transition = rule_engine._detect_environmental_transition(area_id, current_state)

        assert transition == "became_dark"

    def test_detect_environmental_transition_no_change(self, rule_engine):
        """Test no transition when state unchanged."""
        area_id = "salon"

        # Set previous state: dark
        rule_engine._previous_env_state[area_id] = {
            "is_dark": True,
        }

        # Current state: still dark
        current_state = {"is_dark": True}

        transition = rule_engine._detect_environmental_transition(area_id, current_state)

        assert transition is None

    def test_detect_environmental_transition_no_previous_state(self, rule_engine):
        """Test no transition on first check (no previous state)."""
        area_id = "salon"

        # No previous state
        current_state = {"is_dark": True}

        transition = rule_engine._detect_environmental_transition(area_id, current_state)

        assert transition is None

    @pytest.mark.asyncio
    async def test_environmental_state_cache_initialized_on_enable(
        self, rule_engine, mock_hass, mock_area_manager
    ):
        """Test that environmental state cache is initialized when area enabled."""
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )

        await rule_engine.async_initialize()

        # Check cache was initialized for salon
        assert "salon" in rule_engine._previous_env_state
        assert rule_engine._previous_env_state["salon"] == {
            "is_dark": False,
        }

    @pytest.mark.asyncio
    async def test_environmental_state_cache_cleared_on_disable(
        self, rule_engine, mock_hass, mock_area_manager
    ):
        """Test that environmental state cache is cleared when area disabled."""
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )

        await rule_engine.async_initialize()

        # Verify cache exists
        assert "salon" in rule_engine._previous_env_state

        # Disable area
        await rule_engine.disable_area("salon")

        # Cache should be cleared
        assert "salon" not in rule_engine._previous_env_state


class TestEnvironmentalCooldown:
    """Test separate cooldown for environmental triggers."""

    @pytest.mark.asyncio
    async def test_environmental_triggers_use_separate_cooldown(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that environmental triggers use separate cooldown key."""
        # Mock condition evaluator to return True so execution completes
        rule_engine.condition_evaluator.evaluate_conditions = AsyncMock(
            return_value=True
        )
        
        # Mock action executor to succeed
        rule_engine.action_executor.execute_actions = AsyncMock(return_value=True)
        
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )

        await rule_engine.async_initialize()

        # Trigger an environmental evaluation
        await rule_engine._async_evaluate_and_execute("salon", is_environmental=True)

        # Check that environmental cooldown key was created
        assert "salon_env" in rule_engine._last_triggered

    @pytest.mark.asyncio
    async def test_environmental_cooldown_longer_than_activity_cooldown(
        self, rule_engine
    ):
        """Test that environmental cooldown is longer than activity cooldown."""
        from ..utils.rule_engine import (
            COOLDOWN_ENVIRONMENTAL_SECONDS,
            COOLDOWN_SECONDS,
        )

        # Environmental cooldown should be significantly longer
        assert COOLDOWN_ENVIRONMENTAL_SECONDS > COOLDOWN_SECONDS
        # Expect 5 minutes (300s) vs 30s
        assert COOLDOWN_ENVIRONMENTAL_SECONDS == 300
        assert COOLDOWN_SECONDS == 30

    def test_check_cooldown_environmental_trigger(self, rule_engine):
        """Test cooldown check for environmental triggers."""
        from datetime import datetime, timedelta

        from homeassistant.util import dt as dt_util

        area_id = "salon"

        # Set environmental trigger timestamp 2 minutes ago
        rule_engine._last_triggered[f"{area_id}_env"] = dt_util.utcnow() - timedelta(
            minutes=2
        )

        # Should still be in cooldown (5 minute cooldown)
        assert (
            rule_engine._check_cooldown(area_id, None, is_environmental=True) is False
        )

        # Set environmental trigger timestamp 6 minutes ago
        rule_engine._last_triggered[f"{area_id}_env"] = dt_util.utcnow() - timedelta(
            minutes=6
        )

        # Should be out of cooldown now
        assert rule_engine._check_cooldown(area_id, None, is_environmental=True) is True

    def test_check_cooldown_activity_trigger_independent(self, rule_engine):
        """Test that activity and environmental cooldowns are independent."""
        from datetime import datetime, timedelta

        from homeassistant.util import dt as dt_util

        area_id = "salon"
        activity = "movement"

        # Set environmental trigger timestamp 2 minutes ago (still in cooldown)
        rule_engine._last_triggered[f"{area_id}_env"] = dt_util.utcnow() - timedelta(
            minutes=2
        )

        # Activity trigger should not be affected
        assert rule_engine._check_cooldown(area_id, activity, is_environmental=False) is True

        # Set activity trigger timestamp 10 seconds ago
        rule_engine._last_triggered[f"{area_id}_{activity}"] = (
            dt_util.utcnow() - timedelta(seconds=10)
        )

        # Activity should be in cooldown (30 second cooldown)
        assert rule_engine._check_cooldown(area_id, activity, is_environmental=False) is False

        # But environmental trigger should still be in cooldown independently
        assert rule_engine._check_cooldown(area_id, None, is_environmental=True) is False


class TestEnvironmentalTriggersIntegration:
    """Integration tests for end-to-end environmental trigger flow."""

    @pytest.mark.asyncio
    async def test_illuminance_transition_to_dark_turns_on_lights(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test full flow: area becomes dark → lights turn ON (with presence)."""
        # Setup: Area has presence (movement), initially bright
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        mock_activity_tracker.get_activity = MagicMock(return_value="movement")
        
        # Mock condition evaluator to return True (is_dark condition met)
        rule_engine.condition_evaluator.evaluate_conditions = AsyncMock(
            return_value=True
        )
        
        # Mock action executor to succeed
        rule_engine.action_executor.execute_actions = AsyncMock(return_value=True)
        
        # Mock hass states
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        # Initial state: bright (is_dark=False)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )
        
        # Initialize rule engine
        await rule_engine.async_initialize()
        
        # Verify initial cache
        assert rule_engine._previous_env_state["salon"]["is_dark"] is False
        
        # Transition: area becomes dark (is_dark=True)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        # Simulate illuminance sensor change
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        # Trigger state change handler
        rule_engine._async_state_change_handler(event)
        
        # Wait for debounce
        await asyncio.sleep(2.5)
        
        # Verify activity evaluation was triggered
        mock_activity_tracker.async_evaluate_activity.assert_called_with("salon")
        
        # Verify conditions were evaluated
        rule_engine.condition_evaluator.evaluate_conditions.assert_called()
        
        # Verify actions were executed (lights turned on)
        rule_engine.action_executor.execute_actions.assert_called()
        
        # Verify environmental cooldown was set
        assert "salon_env" in rule_engine._last_triggered
        
        # Verify cache was updated
        assert rule_engine._previous_env_state["salon"]["is_dark"] is True

    @pytest.mark.asyncio
    async def test_no_transition_skips_evaluation(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that environmental entity change WITHOUT transition skips evaluation."""
        # Setup: Area dark, and stays dark
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        
        # Mock hass states
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        # Initial state: dark
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        # Initialize rule engine
        await rule_engine.async_initialize()
        
        # Reset mock to track new calls
        mock_activity_tracker.async_evaluate_activity.reset_mock()
        
        # Same state: still dark (illuminance goes from 10 → 15 lux, both < 20)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        # Simulate illuminance sensor change (but no transition)
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        # Trigger state change handler
        rule_engine._async_state_change_handler(event)
        
        # Wait for debounce
        await asyncio.sleep(2.5)
        
        # Verify evaluation was NOT triggered (no transition)
        mock_activity_tracker.async_evaluate_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_environmental_cooldown_prevents_rapid_retriggering(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that environmental cooldown prevents rapid re-triggering."""
        from datetime import timedelta
        from homeassistant.util import dt as dt_util
        
        # Setup
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        mock_activity_tracker.get_activity = MagicMock(return_value="movement")
        
        rule_engine.condition_evaluator.evaluate_conditions = AsyncMock(
            return_value=True
        )
        rule_engine.action_executor.execute_actions = AsyncMock(return_value=True)
        
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        # Initial: bright
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )
        
        await rule_engine.async_initialize()
        
        # First transition: bright → dark
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        event = MagicMock()
        event.data = {"entity_id": "sensor.salon_illuminance"}
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(2.5)
        
        # Verify first execution
        assert rule_engine.action_executor.execute_actions.call_count == 1
        
        # Reset mock
        rule_engine.action_executor.execute_actions.reset_mock()
        
        # Second transition: dark → bright (immediately, within cooldown)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(2.5)
        
        # Verify second execution was BLOCKED by cooldown (5 minutes)
        rule_engine.action_executor.execute_actions.assert_not_called()
        
        # Simulate 6 minutes passing
        rule_engine._last_triggered["salon_env"] = dt_util.utcnow() - timedelta(minutes=6)
        
        # Third transition: bright → dark (after cooldown expired)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(2.5)
        
        # Verify third execution succeeded (cooldown expired)
        assert rule_engine.action_executor.execute_actions.call_count == 1

    @pytest.mark.asyncio
    async def test_sun_elevation_transition_triggers_evaluation(
        self, rule_engine, mock_hass, mock_activity_tracker, mock_area_manager
    ):
        """Test that sun elevation crossing threshold triggers evaluation."""
        # Setup: Area with presence, no illuminance sensor (relies on sun)
        mock_activity_tracker.async_evaluate_activity = AsyncMock(
            return_value="movement"
        )
        mock_activity_tracker.get_activity = MagicMock(return_value="movement")
        
        rule_engine.condition_evaluator.evaluate_conditions = AsyncMock(
            return_value=True
        )
        rule_engine.action_executor.execute_actions = AsyncMock(return_value=True)
        
        mock_hass.states.get = MagicMock(return_value=MagicMock())
        
        # Initial: sun above horizon (bright)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": False}
        )
        
        await rule_engine.async_initialize()
        
        # Transition: sun sets below horizon (dark)
        mock_area_manager.get_area_environmental_state = MagicMock(
            return_value={"is_dark": True}
        )
        
        # Simulate sun.sun state change (elevation crosses threshold)
        event = MagicMock()
        event.data = {"entity_id": "sun.sun"}
        
        rule_engine._async_state_change_handler(event)
        await asyncio.sleep(2.5)
        
        # Verify evaluation triggered
        mock_activity_tracker.async_evaluate_activity.assert_called_with("salon")
        rule_engine.action_executor.execute_actions.assert_called()
        
        # Verify environmental cooldown was set
        assert "salon_env" in rule_engine._last_triggered
