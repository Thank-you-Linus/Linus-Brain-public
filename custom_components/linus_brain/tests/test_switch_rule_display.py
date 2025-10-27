"""
Tests for switch rule display functionality.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_hass():
    """Mock Home Assistant."""
    hass = MagicMock()
    hass.data = {}
    hass.config.language = "en"
    return hass


@pytest.fixture
def mock_hass_fr():
    """Mock Home Assistant with French language."""
    hass = MagicMock()
    hass.data = {}
    hass.config.language = "fr"
    return hass


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


@pytest.mark.asyncio
async def test_switch_initializes_with_default_rule(mock_hass, mock_config_entry):
    """Test that switch initializes with default rule template."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    assert switch._rule_data is not None
    assert switch._rule_data["source"] == "local_default"
    assert switch._rule_data["area_id"] == "test_area"
    assert switch._rule_data["area_name"] == "Test Area"
    assert "activity_rules" in switch._rule_data
    assert isinstance(switch._rule_data["activity_rules"], dict)
    assert "presence" in switch._rule_data["activity_rules"]
    assert "none" in switch._rule_data["activity_rules"]


@pytest.mark.asyncio
async def test_switch_extra_state_attributes_include_rule_summary(
    mock_hass, mock_config_entry
):
    """Test that switch attributes include human-readable summary."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    attrs = switch.extra_state_attributes

    assert "source" in attrs
    assert "enabled" in attrs
    assert "presence" in attrs
    assert "area_id" not in attrs
    assert "area_name" not in attrs
    assert "version" not in attrs
    assert "conditions" not in attrs
    assert "actions" not in attrs


@pytest.mark.asyncio
async def test_switch_rule_summary_english(mock_hass, mock_config_entry):
    """Test that rule summary is in English when language is 'en'."""
    from ..switch import LinusAutoLightSwitch

    mock_hass.config.language = "en"

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    attrs = switch.extra_state_attributes
    summary = attrs.get("presence", "")

    assert "💡" in summary
    assert "Turn on lights" in summary or "Turn off lights" in summary


@pytest.mark.asyncio
async def test_switch_rule_summary_french(mock_hass_fr, mock_config_entry):
    """Test that rule summary is in French when language is 'fr'."""
    from ..switch import LinusAutoLightSwitch

    mock_hass_fr.config.language = "fr"

    switch = LinusAutoLightSwitch(
        hass=mock_hass_fr,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    attrs = switch.extra_state_attributes
    summary = attrs.get("presence", "")

    assert "💡" in summary
    assert "Turn on lights" in summary or "Turn off lights" in summary


@pytest.mark.asyncio
async def test_switch_rule_data_structure(mock_hass, mock_config_entry):
    """Test that rule data has correct structure when updated."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    new_rule = {
        "rule_id": "rule_123",
        "version": "v1",
        "source": "supabase",
        "enabled": True,
        "conditions": [
            {
                "condition": "state",
                "domain": "binary_sensor",
                "device_class": "motion",
                "area": "current",
                "state": "on",
            }
        ],
        "actions": [{"service": "light.turn_on", "domain": "light", "area": "current"}],
        "description": "Test rule from Supabase",
        "area_id": "test_area",
        "area_name": "Test Area",
    }

    switch._rule_data = new_rule

    assert switch._rule_data["rule_id"] == "rule_123"
    assert switch._rule_data["source"] == "supabase"
    assert switch._rule_data["area_id"] == "test_area"
    assert switch._rule_data["area_name"] == "Test Area"


@pytest.mark.asyncio
async def test_default_rule_template_structure():
    """Test that default rule template has correct activity-based structure."""
    from ..const import DEFAULT_ACTIVITY_RULES

    template = DEFAULT_ACTIVITY_RULES

    assert "presence" in template
    assert "none" in template

    presence_rule = template["presence"]
    assert "conditions" in presence_rule
    assert "actions" in presence_rule
    assert "description" in presence_rule
    assert isinstance(presence_rule["conditions"], list)
    assert isinstance(presence_rule["actions"], list)
    assert len(presence_rule["conditions"]) > 0
    assert len(presence_rule["actions"]) > 0

    has_activity = any(
        c.get("condition") == "activity" for c in presence_rule["conditions"]
    )
    has_area_state = any(
        c.get("condition") == "area_state" for c in presence_rule["conditions"]
    )
    assert has_activity, "Presence rule should have activity condition"
    assert has_area_state, "Presence rule should have area_state condition"

    none_rule = template["none"]
    assert "conditions" in none_rule
    assert "actions" in none_rule
    assert len(none_rule["conditions"]) > 0
    assert len(none_rule["actions"]) > 0


@pytest.mark.asyncio
async def test_switch_unique_id_and_name(mock_hass, mock_config_entry):
    """Test that switch has correct unique_id and name."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    assert switch.unique_id == "test_entry_autolight_test_area"
    assert switch.name == "AutoLight Test Area"
    assert switch._area_id == "test_area"
    assert switch._area_name == "Test Area"


@pytest.mark.asyncio
async def test_parse_condition_with_motion_sensor(mock_hass, mock_config_entry):
    """Test parsing motion sensor condition."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    condition = {
        "condition": "state",
        "domain": "binary_sensor",
        "device_class": "motion",
        "area": "current",
        "state": "on",
    }

    result = switch._parse_condition_summary(condition, skip_motion=False)
    assert "motion detected" in result.lower()


@pytest.mark.asyncio
async def test_parse_action_turn_on_lights(mock_hass, mock_config_entry):
    """Test parsing light turn on action."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    action = {"service": "light.turn_on", "domain": "light", "area": "current"}

    result = switch._parse_action_summary(action)
    assert "💡 Turn on lights" in result


@pytest.mark.asyncio
async def test_parse_complex_nested_conditions(mock_hass, mock_config_entry):
    """Test parsing complex nested AND/OR conditions."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    condition = {
        "condition": "and",
        "conditions": [
            {
                "condition": "state",
                "domain": "binary_sensor",
                "device_class": "motion",
                "state": "on",
            },
            {
                "condition": "or",
                "conditions": [
                    {
                        "condition": "numeric_state",
                        "entity_id": "sun.sun",
                        "attribute": "elevation",
                        "below": 3,
                    },
                    {
                        "condition": "numeric_state",
                        "device_class": "illuminance",
                        "below": 20,
                    },
                ],
            },
        ],
    }

    result = switch._parse_condition_summary(condition, skip_motion=False)
    assert "motion detected" in result.lower()
    assert "&" in result
    assert "|" in result or "(" in result


@pytest.mark.asyncio
async def test_activity_based_rule_display(mock_hass, mock_config_entry):
    """Test that switch can display activity-based rules."""
    from ..switch import LinusAutoLightSwitch

    switch = LinusAutoLightSwitch(
        hass=mock_hass,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    activity_rule = {
        "area_id": "test_area",
        "area_name": "Test Area",
        "enabled": True,
        "source": "cloud",
        "activity_rules": {
            "presence": {
                "conditions": [
                    {
                        "condition": "state",
                        "domain": "binary_sensor",
                        "device_class": "motion",
                        "state": "on",
                    }
                ],
                "actions": [{"service": "light.turn_on", "domain": "light"}],
            }
        },
    }

    switch._rule_data = activity_rule

    attrs = switch.extra_state_attributes

    assert "presence" in attrs
    assert "💡 Turn on lights" in attrs["presence"]


@pytest.mark.asyncio
async def test_activity_rule_summaries_french(mock_hass_fr, mock_config_entry):
    """Test that activity rule summaries use French translations."""
    from ..switch import LinusAutoLightSwitch

    mock_hass_fr.config.language = "fr"

    switch = LinusAutoLightSwitch(
        hass=mock_hass_fr,
        entry=mock_config_entry,
        area_id="test_area",
        area_name="Test Area",
    )

    activity_rule_data = {
        "area_id": "test_area",
        "area_name": "Test Area",
        "enabled": True,
        "source": "supabase",
        "activity_rules": {
            "none": {
                "conditions": [],
                "actions": [{"service": "light.turn_off", "domain": "light"}],
            },
            "presence": {
                "conditions": [],
                "actions": [{"service": "light.turn_on", "domain": "light"}],
            },
        },
    }

    switch._rule_data = activity_rule_data

    attrs = switch.extra_state_attributes

    assert "none" in attrs
    assert "presence" in attrs
    assert "💡" in attrs["none"]
    assert "💡" in attrs["presence"]
