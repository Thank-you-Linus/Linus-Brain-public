"""
Comprehensive tests for Feature Flag Manager.

Tests cover:
- Feature flag initialization and loading
- Setting and getting feature states
- Persistence to AppStorage
- Metrics collection
- Validation methods
- System health checks
- Switch entity integration
- Edge cases and error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ..utils.feature_flag_manager import FeatureFlagManager, ValidationResult
from ..utils.app_storage import AppStorage
from ..const import AVAILABLE_FEATURES


@pytest.fixture
def mock_app_storage():
    """Create a mock AppStorage instance."""
    storage = MagicMock(spec=AppStorage)
    storage._data = {"feature_states": {}}
    storage.async_load = AsyncMock(return_value={"feature_states": {}})
    storage.async_save = AsyncMock()
    return storage


@pytest.fixture
def feature_flag_manager(mock_app_storage):
    """Create a FeatureFlagManager instance with mocked storage."""
    manager = FeatureFlagManager(app_storage=mock_app_storage)
    return manager


@pytest.fixture
def initialized_manager(mock_app_storage):
    """Create an initialized FeatureFlagManager with test data."""
    manager = FeatureFlagManager(app_storage=mock_app_storage)
    # Manually set some test data
    manager._area_feature_states = {
        "living_room": {
            "automatic_lighting_enabled": True,
        },
        "kitchen": {
            "automatic_lighting_enabled": False,
        },
    }
    return manager


# ===== INITIALIZATION TESTS =====


@pytest.mark.asyncio
async def test_feature_flag_manager_initializes_with_defaults(feature_flag_manager):
    """Test that FeatureFlagManager initializes with default values."""
    assert feature_flag_manager._area_feature_states == {}
    assert feature_flag_manager._feature_definitions == AVAILABLE_FEATURES
    assert feature_flag_manager._metrics["total_checks"] == 0
    assert feature_flag_manager.app_storage is not None


@pytest.mark.asyncio
async def test_load_feature_states_empty_storage(
    feature_flag_manager, mock_app_storage
):
    """Test loading feature states when storage is empty."""
    all_areas = ["living_room", "kitchen", "bedroom"]

    await feature_flag_manager.load_feature_states(all_areas)

    # Verify all areas were initialized with default False
    for area in all_areas:
        assert area in feature_flag_manager._area_feature_states
        assert (
            "automatic_lighting_enabled"
            in feature_flag_manager._area_feature_states[area]
        )
        assert (
            feature_flag_manager._area_feature_states[area][
                "automatic_lighting_enabled"
            ]
            is False
        )


@pytest.mark.asyncio
async def test_load_feature_states_with_existing_data(
    feature_flag_manager, mock_app_storage
):
    """Test loading feature states when data exists in storage."""
    # Setup existing data
    existing_data = {
        "feature_states": {
            "living_room": {
                "automatic_lighting_enabled": True,
            },
        }
    }
    mock_app_storage.async_load.return_value = existing_data

    all_areas = ["living_room", "kitchen"]
    await feature_flag_manager.load_feature_states(all_areas)

    # Verify existing data was preserved
    assert (
        feature_flag_manager._area_feature_states["living_room"][
            "automatic_lighting_enabled"
        ]
        is True
    )

    # Verify new area was initialized with defaults
    assert (
        feature_flag_manager._area_feature_states["kitchen"][
            "automatic_lighting_enabled"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_load_feature_states_without_storage(mock_app_storage):
    """Test loading feature states when no storage is available."""
    manager = FeatureFlagManager(app_storage=None)

    all_areas = ["living_room"]
    await manager.load_feature_states(all_areas)

    # Should handle gracefully with empty state
    assert manager._area_feature_states == {}


@pytest.mark.asyncio
async def test_load_feature_states_handles_errors(
    feature_flag_manager, mock_app_storage
):
    """Test that load_feature_states handles storage errors gracefully."""
    mock_app_storage.async_load.side_effect = Exception("Storage error")

    all_areas = ["living_room"]
    await feature_flag_manager.load_feature_states(all_areas)

    # Should handle error gracefully
    assert feature_flag_manager._area_feature_states == {}


# ===== FEATURE STATE MANAGEMENT TESTS =====


def test_is_feature_enabled_returns_false_for_disabled_feature(initialized_manager):
    """Test checking if a disabled feature returns False."""
    result = initialized_manager.is_feature_enabled("kitchen", "automatic_lighting")

    assert result is False
    assert initialized_manager._metrics["disabled_checks"] == 1
    assert initialized_manager._metrics["skipped_evaluations"] == 1


def test_is_feature_enabled_returns_true_for_enabled_feature(initialized_manager):
    """Test checking if an enabled feature returns True."""
    result = initialized_manager.is_feature_enabled("living_room", "automatic_lighting")

    assert result is True
    assert initialized_manager._metrics["enabled_checks"] == 1


def test_is_feature_enabled_for_nonexistent_area(feature_flag_manager):
    """Test checking feature for area that doesn't exist."""
    result = feature_flag_manager.is_feature_enabled(
        "nonexistent", "automatic_lighting"
    )

    assert result is False


def test_is_feature_enabled_for_nonexistent_feature(initialized_manager):
    """Test checking feature that doesn't exist."""
    result = initialized_manager.is_feature_enabled(
        "living_room", "nonexistent_feature"
    )

    assert result is False


async def test_set_feature_enabled_creates_new_area(feature_flag_manager):
    """Test setting feature enabled creates new area if needed."""
    await feature_flag_manager.set_feature_enabled("new_area", "automatic_lighting", True)

    assert "new_area" in feature_flag_manager._area_feature_states
    assert (
        feature_flag_manager._area_feature_states["new_area"][
            "automatic_lighting_enabled"
        ]
        is True
    )


async def test_set_feature_enabled_updates_existing_feature(initialized_manager):
    """Test setting feature enabled updates existing feature state."""
    # Initial state is False
    assert (
        initialized_manager.is_feature_enabled("kitchen", "automatic_lighting") is False
    )

    # Enable feature
    await initialized_manager.set_feature_enabled("kitchen", "automatic_lighting", True)

    # Verify state changed
    assert (
        initialized_manager._area_feature_states["kitchen"][
            "automatic_lighting_enabled"
        ]
        is True
    )


async def test_set_feature_enabled_no_change_if_same_value(initialized_manager):
    """Test that setting same value doesn't trigger persistence."""
    # Set to same value (already False)
    await initialized_manager.set_feature_enabled("kitchen", "automatic_lighting", False)

    # persist_feature_states should not be called since value didn't change
    # (The internal check prevents unnecessary persistence)


def test_get_area_feature_states(initialized_manager):
    """Test getting all feature states for an area."""
    states = initialized_manager.get_area_feature_states("living_room")

    assert states == {"automatic_lighting_enabled": True}


def test_get_area_feature_states_nonexistent_area(feature_flag_manager):
    """Test getting feature states for nonexistent area returns empty dict."""
    states = feature_flag_manager.get_area_feature_states("nonexistent")

    assert states == {}


def test_get_all_feature_states(initialized_manager):
    """Test getting all feature states for all areas."""
    all_states = initialized_manager.get_all_feature_states()

    assert "living_room" in all_states
    assert "kitchen" in all_states
    assert all_states["living_room"]["automatic_lighting_enabled"] is True
    assert all_states["kitchen"]["automatic_lighting_enabled"] is False


def test_get_feature_definitions(feature_flag_manager):
    """Test getting feature definitions."""
    definitions = feature_flag_manager.get_feature_definitions()

    assert "automatic_lighting" in definitions
    assert definitions["automatic_lighting"]["name"] == "Automatic Lighting"
    assert definitions["automatic_lighting"]["default_enabled"] is False


# ===== PERSISTENCE TESTS =====


@pytest.mark.asyncio
async def test_persist_feature_states_saves_to_storage(
    initialized_manager, mock_app_storage
):
    """Test that persist_feature_states saves to AppStorage."""
    await initialized_manager.persist_feature_states()

    # Verify async_save was called
    mock_app_storage.async_save.assert_called_once()

    # Verify data was updated in storage
    assert (
        mock_app_storage._data["feature_states"]
        == initialized_manager._area_feature_states
    )


@pytest.mark.asyncio
async def test_persist_feature_states_without_storage():
    """Test persist_feature_states handles missing storage gracefully."""
    manager = FeatureFlagManager(app_storage=None)

    # Should not raise exception
    await manager.persist_feature_states()


@pytest.mark.asyncio
async def test_persist_feature_states_handles_errors(
    initialized_manager, mock_app_storage
):
    """Test that persist_feature_states handles storage errors gracefully."""
    mock_app_storage.async_save.side_effect = Exception("Storage error")

    # Should not raise exception
    await initialized_manager.persist_feature_states()


# ===== METRICS TESTS =====


def test_metrics_increment_on_feature_check(feature_flag_manager):
    """Test that metrics increment correctly on feature checks."""
    # Initial state
    assert feature_flag_manager._metrics["total_checks"] == 0

    # Check feature (disabled)
    feature_flag_manager.is_feature_enabled("test_area", "automatic_lighting")

    assert feature_flag_manager._metrics["total_checks"] == 1
    assert feature_flag_manager._metrics["feature_evaluations"] == 1
    assert feature_flag_manager._metrics["disabled_checks"] == 1


def test_get_metrics_returns_complete_data(initialized_manager):
    """Test that get_metrics returns all expected data."""
    # Perform some checks
    initialized_manager.is_feature_enabled("living_room", "automatic_lighting")
    initialized_manager.is_feature_enabled("kitchen", "automatic_lighting")

    metrics = initialized_manager.get_metrics()

    assert "uptime_seconds" in metrics
    assert metrics["total_checks"] == 2
    assert metrics["enabled_checks"] == 1
    assert metrics["disabled_checks"] == 1
    assert metrics["enabled_areas_count"] == 2
    assert "living_room" in metrics["enabled_areas"]
    assert "kitchen" in metrics["enabled_areas"]
    assert "check_rate" in metrics
    assert "skip_rate" in metrics


def test_reset_metrics(initialized_manager):
    """Test that reset_metrics clears all counters."""
    # Perform some checks
    initialized_manager.is_feature_enabled("living_room", "automatic_lighting")
    initialized_manager.is_feature_enabled("kitchen", "automatic_lighting")

    # Reset
    initialized_manager.reset_metrics()

    # Verify all metrics are reset
    assert initialized_manager._metrics["total_checks"] == 0
    assert initialized_manager._metrics["enabled_checks"] == 0
    assert initialized_manager._metrics["disabled_checks"] == 0
    assert initialized_manager._metrics["feature_evaluations"] == 0


# ===== VALIDATION TESTS =====


@pytest.mark.asyncio
async def test_validate_feature_state_valid_enabled_feature(initialized_manager):
    """Test validation of a valid enabled feature."""
    result = await initialized_manager.validate_feature_state(
        "living_room", "automatic_lighting"
    )

    assert result.is_valid is True
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_validate_feature_state_valid_disabled_feature(initialized_manager):
    """Test validation of a valid but disabled feature."""
    result = await initialized_manager.validate_feature_state(
        "kitchen", "automatic_lighting"
    )

    assert result.is_valid is True
    assert len(result.warnings) >= 1
    assert "disabled" in result.warnings[0].lower()


@pytest.mark.asyncio
async def test_validate_feature_state_nonexistent_feature(initialized_manager):
    """Test validation of a nonexistent feature."""
    result = await initialized_manager.validate_feature_state(
        "living_room", "nonexistent"
    )

    assert result.is_valid is False
    assert len(result.errors) >= 1
    assert "not defined" in result.errors[0].lower()


@pytest.mark.asyncio
async def test_validate_feature_state_nonexistent_area(feature_flag_manager):
    """Test validation for nonexistent area."""
    result = await feature_flag_manager.validate_feature_state(
        "nonexistent", "automatic_lighting"
    )

    assert result.is_valid is True
    assert len(result.warnings) >= 1


@pytest.mark.asyncio
async def test_validation_result_has_issues(feature_flag_manager):
    """Test ValidationResult.has_issues method."""
    result_no_issues = ValidationResult(True, [], [], [])
    assert result_no_issues.has_issues() is False

    result_with_errors = ValidationResult(False, ["error"], [], [])
    assert result_with_errors.has_issues() is True

    result_with_warnings = ValidationResult(True, [], ["warning"], [])
    assert result_with_warnings.has_issues() is True


@pytest.mark.asyncio
async def test_validation_result_get_summary(feature_flag_manager):
    """Test ValidationResult.get_summary method."""
    result_valid = ValidationResult(True, [], [], [])
    assert "✅" in result_valid.get_summary()

    result_warnings = ValidationResult(True, [], ["warning"], [])
    assert "⚠️" in result_warnings.get_summary()

    result_errors = ValidationResult(False, ["error"], [], [])
    assert "❌" in result_errors.get_summary()


# ===== DEBUGGING AND SYSTEM OVERVIEW TESTS =====


def test_get_feature_status_explanation(initialized_manager):
    """Test getting detailed feature status explanation."""
    explanation = initialized_manager.get_feature_status_explanation(
        "living_room", "automatic_lighting"
    )

    assert explanation["area_id"] == "living_room"
    assert explanation["feature_id"] == "automatic_lighting"
    assert explanation["is_enabled"] is True
    assert explanation["feature_name"] == "Automatic Lighting"
    assert "last_check" in explanation


def test_get_system_overview(initialized_manager):
    """Test getting comprehensive system overview."""
    overview = initialized_manager.get_system_overview()

    assert "timestamp" in overview
    assert "system_health" in overview
    assert "metrics" in overview
    assert "feature_definitions" in overview
    assert "area_feature_states" in overview
    assert "issues" in overview
    assert "recommendations" in overview


def test_system_health_assessment(feature_flag_manager):
    """Test system health assessment."""
    health = feature_flag_manager._assess_system_health()

    assert "overall_status" in health
    assert "checks" in health
    assert "score" in health
    assert "feature_definitions" in health["checks"]


def test_export_debug_data_json(initialized_manager):
    """Test exporting debug data as JSON."""
    json_data = initialized_manager.export_debug_data(format_type="json")

    assert json_data.startswith("{")
    assert "timestamp" in json_data
    assert "system_health" in json_data


def test_export_debug_data_csv(initialized_manager):
    """Test exporting debug data as CSV."""
    csv_data = initialized_manager.export_debug_data(format_type="csv")

    assert csv_data.startswith("area_id,feature_id,enabled")
    assert "living_room" in csv_data
    assert "kitchen" in csv_data


def test_export_debug_data_txt(initialized_manager):
    """Test exporting debug data as TXT."""
    txt_data = initialized_manager.export_debug_data(format_type="txt")

    assert "Feature Flag Debug Report" in txt_data
    assert "System Health" in txt_data
    assert "living_room" in txt_data


def test_export_debug_data_invalid_format(initialized_manager):
    """Test exporting debug data with invalid format raises error."""
    with pytest.raises(ValueError, match="Unsupported format"):
        initialized_manager.export_debug_data(format_type="invalid")


# ===== INTEGRATION TESTS WITH SWITCH =====


@pytest.mark.asyncio
async def test_feature_flag_integration_with_switch_turn_on(feature_flag_manager):
    """Test feature flag manager integration when switch is turned on."""
    # Initial state: disabled
    assert (
        feature_flag_manager.is_feature_enabled("test_area", "automatic_lighting")
        is False
    )

    # Simulate switch turning on
    await feature_flag_manager.set_feature_enabled(
        "test_area", "automatic_lighting", True
    )

    # Verify state changed
    assert (
        feature_flag_manager.is_feature_enabled("test_area", "automatic_lighting")
        is True
    )


@pytest.mark.asyncio
async def test_feature_flag_integration_with_switch_turn_off(initialized_manager):
    """Test feature flag manager integration when switch is turned off."""
    # Initial state: enabled
    assert (
        initialized_manager.is_feature_enabled("living_room", "automatic_lighting")
        is True
    )

    # Simulate switch turning off
    await initialized_manager.set_feature_enabled(
        "living_room", "automatic_lighting", False
    )

    # Verify state changed
    assert (
        initialized_manager.is_feature_enabled("living_room", "automatic_lighting")
        is False
    )


@pytest.mark.asyncio
async def test_multiple_areas_independent_states(feature_flag_manager):
    """Test that multiple areas can have independent feature states."""
    # Set different states for different areas
    await feature_flag_manager.set_feature_enabled("area1", "automatic_lighting", True)
    await feature_flag_manager.set_feature_enabled("area2", "automatic_lighting", False)
    await feature_flag_manager.set_feature_enabled("area3", "automatic_lighting", True)

    # Verify independence
    assert (
        feature_flag_manager.is_feature_enabled("area1", "automatic_lighting") is True
    )
    assert (
        feature_flag_manager.is_feature_enabled("area2", "automatic_lighting") is False
    )
    assert (
        feature_flag_manager.is_feature_enabled("area3", "automatic_lighting") is True
    )


# ===== EDGE CASES AND ERROR HANDLING =====


def test_concurrent_feature_checks(initialized_manager):
    """Test that concurrent feature checks work correctly."""
    # Simulate concurrent checks
    results = []
    for _ in range(100):
        results.append(
            initialized_manager.is_feature_enabled("living_room", "automatic_lighting")
        )

    # All should return True
    assert all(results)
    assert initialized_manager._metrics["total_checks"] == 100


@pytest.mark.asyncio
async def test_feature_toggle_multiple_times(initialized_manager):
    """Test toggling feature multiple times works correctly."""
    area = "test_area"
    feature = "automatic_lighting"

    # Toggle on and off multiple times
    for i in range(10):
        expected_state = i % 2 == 0
        await initialized_manager.set_feature_enabled(area, feature, expected_state)
        assert (
            initialized_manager.is_feature_enabled(area, feature) == expected_state
        )


@pytest.mark.asyncio
async def test_load_then_modify_then_persist(feature_flag_manager, mock_app_storage):
    """Test complete flow: load, modify, persist."""
    # Load initial state
    await feature_flag_manager.load_feature_states(["area1", "area2"])

    # Modify states
    await feature_flag_manager.set_feature_enabled("area1", "automatic_lighting", True)
    await feature_flag_manager.set_feature_enabled("area2", "automatic_lighting", False)

    # Verify storage was updated
    mock_app_storage.async_save.assert_called()
    assert (
        mock_app_storage._data["feature_states"]["area1"]["automatic_lighting_enabled"]
        is True
    )
    assert (
        mock_app_storage._data["feature_states"]["area2"]["automatic_lighting_enabled"]
        is False
    )


def test_log_feature_status(initialized_manager):
    """Test logging feature status doesn't raise errors."""
    # Should not raise any exceptions
    initialized_manager.log_feature_status(
        "living_room", "automatic_lighting", "test context"
    )


def test_area_manager_setter(feature_flag_manager):
    """Test setting area manager reference."""
    mock_area_manager = MagicMock()
    feature_flag_manager.set_area_manager(mock_area_manager)

    assert hasattr(feature_flag_manager, "_area_manager")
    assert feature_flag_manager._area_manager == mock_area_manager


def test_app_storage_setter(feature_flag_manager):
    """Test setting app storage reference."""
    new_storage = MagicMock(spec=AppStorage)
    feature_flag_manager.set_app_storage(new_storage)

    assert feature_flag_manager.app_storage == new_storage
