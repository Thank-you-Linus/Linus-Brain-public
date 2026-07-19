"""
Test cleanup of group entities orphaned by their removal from Linus Brain.

binary_sensor.linus_brain_presence_detection_* and light.linus_brain_all_lights_*
were removed from Brain (superseded by Linus Dashboard's own native group
entities). PLATFORMS no longer includes BINARY_SENSOR/LIGHT, so these entities
never get recreated on startup and would otherwise sit in the entity registry
forever as permanently "unavailable". async_cleanup_orphaned_group_entities
removes them on the first startup after upgrade.
"""

from unittest.mock import MagicMock, patch

import pytest


class MockEntityEntry:
    """Mock entity registry entry."""

    def __init__(self, entity_id, unique_id, config_entry_id="test_entry"):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


@pytest.fixture
def mock_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


@pytest.mark.asyncio
async def test_cleanup_removes_orphaned_presence_and_light_group_entities(
    hass, mock_entry
):
    """Orphaned presence-detection and all-lights entities are removed."""
    from .. import async_cleanup_orphaned_group_entities

    orphaned = [
        MockEntityEntry(
            "binary_sensor.linus_brain_presence_detection_salon",
            "linus_brain_presence_detection_salon",
        ),
        MockEntityEntry(
            "light.linus_brain_all_lights_salon",
            "linus_brain_all_lights_salon",
        ),
    ]
    survivors = [
        MockEntityEntry(
            "sensor.linus_brain_activity_salon", "linus_brain_activity_salon"
        ),
        MockEntityEntry(
            "switch.linus_brain_feature_automatic_lighting_salon",
            "linus_brain_feature_automatic_lighting_salon",
        ),
    ]

    registry = MagicMock()
    registry.entities.values = MagicMock(return_value=orphaned + survivors)
    removed = []
    registry.async_remove = MagicMock(side_effect=removed.append)

    with patch("linus_brain.er.async_get", return_value=registry):
        await async_cleanup_orphaned_group_entities(hass, mock_entry)

    assert set(removed) == {
        "binary_sensor.linus_brain_presence_detection_salon",
        "light.linus_brain_all_lights_salon",
    }


@pytest.mark.asyncio
async def test_cleanup_ignores_entities_from_other_config_entries(hass, mock_entry):
    """Only entities belonging to this config entry are touched."""
    from .. import async_cleanup_orphaned_group_entities

    other_entry_entity = MockEntityEntry(
        "binary_sensor.linus_brain_presence_detection_cuisine",
        "linus_brain_presence_detection_cuisine",
        config_entry_id="some_other_entry",
    )

    registry = MagicMock()
    registry.entities.values = MagicMock(return_value=[other_entry_entity])
    registry.async_remove = MagicMock()

    with patch("linus_brain.er.async_get", return_value=registry):
        await async_cleanup_orphaned_group_entities(hass, mock_entry)

    registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_is_a_noop_when_nothing_orphaned(hass, mock_entry):
    """No removals when the registry has no leftover group entities."""
    from .. import async_cleanup_orphaned_group_entities

    survivors = [
        MockEntityEntry(
            "sensor.linus_brain_activity_salon", "linus_brain_activity_salon"
        ),
    ]

    registry = MagicMock()
    registry.entities.values = MagicMock(return_value=survivors)
    registry.async_remove = MagicMock()

    with patch("linus_brain.er.async_get", return_value=registry):
        await async_cleanup_orphaned_group_entities(hass, mock_entry)

    registry.async_remove.assert_not_called()
