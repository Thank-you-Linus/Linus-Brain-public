"""
Unit tests for AreaManager.

Tests the area management logic including:
- Entity discovery and grouping by area
- Presence detection capabilities
- Environmental state (illuminance, temperature, humidity, sun elevation)
- Area eligibility checks (tracking, light automation)
- Entity queries by area
"""

from unittest.mock import MagicMock, PropertyMock

import pytest
from homeassistant.core import State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..utils.area_manager import AreaManager


@pytest.fixture
def hass():
    """Mock Home Assistant instance."""
    hass_mock = MagicMock()
    hass_mock.states = MagicMock()
    hass_mock.states.get = MagicMock(return_value=None)
    return hass_mock


@pytest.fixture
def area_registry_mock():
    """Mock area registry with test areas."""
    registry = MagicMock(spec=ar.AreaRegistry)

    living_room = MagicMock()
    living_room.id = "living_room"
    living_room.name = "Living Room"
    living_room.temperature_entity_id = None
    living_room.humidity_entity_id = None

    bedroom = MagicMock()
    bedroom.id = "bedroom"
    bedroom.name = "Bedroom"
    bedroom.temperature_entity_id = "sensor.bedroom_temperature"
    bedroom.humidity_entity_id = "sensor.bedroom_humidity"

    kitchen = MagicMock()
    kitchen.id = "kitchen"
    kitchen.name = "Kitchen"
    kitchen.temperature_entity_id = None
    kitchen.humidity_entity_id = None

    registry.async_get_area = MagicMock(
        side_effect=lambda area_id: {
            "living_room": living_room,
            "bedroom": bedroom,
            "kitchen": kitchen,
        }.get(area_id)
    )

    registry.async_list_areas = MagicMock(return_value=[living_room, bedroom, kitchen])

    return registry


def _create_mock_entity(entity_id, area_id, device_id, original_device_class):
    """Helper to create mock entity registry entry."""
    entity = MagicMock(spec=er.RegistryEntry)
    entity.entity_id = entity_id
    entity.domain = entity_id.split(".")[0]
    entity.area_id = area_id
    entity.device_id = device_id
    entity.original_device_class = original_device_class
    entity.device_class = original_device_class
    return entity


@pytest.fixture
def entity_registry_mock():
    """Mock entity registry with test entities."""
    registry = MagicMock(spec=er.EntityRegistry)

    entities = {
        "binary_sensor.living_room_motion": _create_mock_entity(
            "binary_sensor.living_room_motion", "living_room", None, "motion"
        ),
        "binary_sensor.living_room_presence": _create_mock_entity(
            "binary_sensor.living_room_presence", "living_room", None, "presence"
        ),
        "sensor.living_room_illuminance": _create_mock_entity(
            "sensor.living_room_illuminance", "living_room", None, "illuminance"
        ),
        "light.living_room": _create_mock_entity(
            "light.living_room", "living_room", None, None
        ),
        "media_player.living_room_tv": _create_mock_entity(
            "media_player.living_room_tv", "living_room", None, None
        ),
        "binary_sensor.bedroom_motion": _create_mock_entity(
            "binary_sensor.bedroom_motion", "bedroom", None, "motion"
        ),
        "sensor.bedroom_temperature": _create_mock_entity(
            "sensor.bedroom_temperature", "bedroom", None, "temperature"
        ),
        "sensor.bedroom_humidity": _create_mock_entity(
            "sensor.bedroom_humidity", "bedroom", None, "humidity"
        ),
        "light.bedroom": _create_mock_entity("light.bedroom", "bedroom", None, None),
        "sensor.kitchen_temperature": _create_mock_entity(
            "sensor.kitchen_temperature", "kitchen", None, "temperature"
        ),
        "light.kitchen": _create_mock_entity("light.kitchen", "kitchen", None, None),
    }

    type(registry).entities = PropertyMock(return_value=entities)
    registry.async_get = MagicMock(
        side_effect=lambda entity_id: entities.get(entity_id)
    )

    return registry


@pytest.fixture
def device_registry_mock():
    """Mock device registry."""
    registry = MagicMock(spec=dr.DeviceRegistry)
    registry.async_get = MagicMock(return_value=None)
    return registry


@pytest.fixture
def area_manager(
    hass, area_registry_mock, entity_registry_mock, device_registry_mock, monkeypatch
):
    """Create AreaManager instance with mocked registries."""
    monkeypatch.setattr(
        "homeassistant.helpers.area_registry.async_get", lambda h: area_registry_mock
    )
    monkeypatch.setattr(
        "homeassistant.helpers.entity_registry.async_get",
        lambda h: entity_registry_mock,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.device_registry.async_get",
        lambda h: device_registry_mock,
    )

    return AreaManager(hass)


class TestAreaManagerEntityDiscovery:
    """Test entity discovery and grouping."""

    def test_get_monitored_entities_groups_by_area(self, area_manager):
        """Test that monitored entities are correctly grouped by area."""
        result = area_manager._get_monitored_entities()

        assert "living_room" in result
        assert "bedroom" in result

        living_room_entities = result["living_room"]
        assert "binary_sensor.living_room_motion" in living_room_entities
        assert "binary_sensor.living_room_presence" in living_room_entities
        assert "sensor.living_room_illuminance" in living_room_entities
        assert "media_player.living_room_tv" in living_room_entities

        bedroom_entities = result["bedroom"]
        assert "binary_sensor.bedroom_motion" in bedroom_entities

    def test_get_monitored_entities_excludes_lights(self, area_manager):
        """Test that light entities are not included in monitored entities."""
        result = area_manager._get_monitored_entities()

        living_room_entities = result["living_room"]
        assert "light.living_room" not in living_room_entities

        bedroom_entities = result["bedroom"]
        assert "light.bedroom" not in bedroom_entities

    def test_get_monitored_entities_filters_by_device_class(self, area_manager):
        """Test that only entities with correct device class are included."""
        result = area_manager._get_monitored_entities()

        living_room_entities = result["living_room"]

        assert "binary_sensor.living_room_motion" in living_room_entities
        assert "binary_sensor.living_room_presence" in living_room_entities


class TestAreaManagerPresenceDetection:
    """Test presence detection capabilities."""

    def test_has_presence_detection_returns_true_for_area_with_motion(
        self, area_manager
    ):
        """Test that area with motion sensor has presence detection."""
        assert area_manager.has_presence_detection("living_room") is True
        assert area_manager.has_presence_detection("bedroom") is True

    def test_has_presence_detection_returns_false_for_area_without_sensors(
        self, area_manager
    ):
        """Test that area without presence sensors returns False."""
        assert area_manager.has_presence_detection("kitchen") is False

    def test_get_area_presence_binary_returns_true_when_motion_active(
        self, area_manager, hass
    ):
        """Test binary presence detection with active motion sensor."""
        motion_state = State("binary_sensor.living_room_motion", "on")
        hass.states.get = MagicMock(return_value=motion_state)

        result = area_manager.get_area_presence_binary("living_room")
        assert result is True

    def test_get_area_presence_binary_returns_false_when_no_motion(
        self, area_manager, hass
    ):
        """Test binary presence detection with inactive sensors."""
        motion_state = State("binary_sensor.living_room_motion", "off")
        hass.states.get = MagicMock(return_value=motion_state)

        result = area_manager.get_area_presence_binary("living_room")
        assert result is False

    def test_get_area_presence_binary_checks_multiple_sensors(self, area_manager, hass):
        """Test that binary presence checks all presence sensors."""

        def get_state(entity_id):
            states = {
                "binary_sensor.living_room_motion": State(
                    "binary_sensor.living_room_motion", "off"
                ),
                "binary_sensor.living_room_presence": State(
                    "binary_sensor.living_room_presence", "on"
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = area_manager.get_area_presence_binary("living_room")
        assert result is True


class TestAreaManagerEnvironmentalState:
    """Test environmental state readings."""

    def test_get_area_illuminance_returns_lux_value(self, area_manager, hass):
        """Test getting illuminance from area sensor."""
        lux_state = State(
            "sensor.living_room_illuminance",
            "50.5",
            attributes={"device_class": "illuminance"},
        )
        hass.states.get = MagicMock(return_value=lux_state)

        result = area_manager.get_area_illuminance("living_room")
        assert result == 50.5

    def test_get_area_illuminance_returns_none_when_no_sensor(self, area_manager, hass):
        """Test illuminance returns None when no sensor available."""
        hass.states.get = MagicMock(return_value=None)

        result = area_manager.get_area_illuminance("bedroom")
        assert result is None

    def test_get_area_illuminance_averages_multiple_sensors(
        self, area_manager, entity_registry_mock, hass
    ):
        """Test that illuminance averages values from multiple sensors."""
        entities = entity_registry_mock.entities.copy()
        entities["sensor.living_room_illuminance_2"] = _create_mock_entity(
            "sensor.living_room_illuminance_2", "living_room", None, "illuminance"
        )
        type(entity_registry_mock).entities = PropertyMock(return_value=entities)

        def get_state(entity_id):
            states = {
                "sensor.living_room_illuminance": State(
                    "sensor.living_room_illuminance",
                    "50",
                    attributes={"device_class": "illuminance"},
                ),
                "sensor.living_room_illuminance_2": State(
                    "sensor.living_room_illuminance_2",
                    "100",
                    attributes={"device_class": "illuminance"},
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = area_manager.get_area_illuminance("living_room")
        assert result == 75.0

    def test_get_sun_elevation_returns_degrees(self, area_manager, hass):
        """Test getting sun elevation angle."""
        sun_state = State("sun.sun", "above_horizon", attributes={"elevation": 45.5})
        hass.states.get = MagicMock(return_value=sun_state)

        result = area_manager.get_sun_elevation()
        assert result == 45.5

    def test_get_sun_elevation_returns_none_when_unavailable(self, area_manager, hass):
        """Test sun elevation returns None when sun entity unavailable."""
        hass.states.get = MagicMock(return_value=None)

        result = area_manager.get_sun_elevation()
        assert result is None

    def test_get_area_temperature_uses_configured_sensor(self, area_manager, hass):
        """Test that configured temperature sensor takes priority."""
        temp_state = State(
            "sensor.bedroom_temperature",
            "22.5",
            attributes={"device_class": "temperature"},
        )
        hass.states.get = MagicMock(return_value=temp_state)

        result = area_manager.get_area_temperature("bedroom")
        assert result == 22.5

    def test_get_area_temperature_averages_when_no_configured_sensor(
        self, area_manager, hass
    ):
        """Test that temperature returns None when area has no temperature sensors in MONITORED_DOMAINS."""
        result = area_manager.get_area_temperature("kitchen")
        assert result is None

    def test_get_area_humidity_uses_configured_sensor(self, area_manager, hass):
        """Test that configured humidity sensor takes priority."""
        humidity_state = State(
            "sensor.bedroom_humidity", "65.5", attributes={"device_class": "humidity"}
        )
        hass.states.get = MagicMock(return_value=humidity_state)

        result = area_manager.get_area_humidity("bedroom")
        assert result == 65.5

    def test_get_area_environmental_state_computes_is_dark(self, area_manager, hass):
        """Test that is_dark is True when illuminance < 20 or sun < 3."""

        def get_state(entity_id):
            states = {
                "sensor.living_room_illuminance": State(
                    "sensor.living_room_illuminance",
                    "15",
                    attributes={"device_class": "illuminance"},
                ),
                "sun.sun": State(
                    "sun.sun", "below_horizon", attributes={"elevation": -10}
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = area_manager.get_area_environmental_state("living_room")
        assert result["is_dark"] is True
        assert result["illuminance"] == 15.0
        assert result["sun_elevation"] == -10.0

    def test_get_area_environmental_state_computes_is_bright(self, area_manager, hass):
        """Test that is_bright is True when illuminance > 100 and sun > 10."""

        def get_state(entity_id):
            states = {
                "sensor.living_room_illuminance": State(
                    "sensor.living_room_illuminance",
                    "150",
                    attributes={"device_class": "illuminance"},
                ),
                "sun.sun": State(
                    "sun.sun", "above_horizon", attributes={"elevation": 45}
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = area_manager.get_area_environmental_state("living_room")
        assert result["is_bright"] is True
        assert result["illuminance"] == 150.0
        assert result["sun_elevation"] == 45.0


class TestAreaManagerAreaQueries:
    """Test area eligibility and query methods."""

    def test_get_activity_tracking_areas_returns_areas_with_presence(
        self, area_manager
    ):
        """Test getting areas with activity tracking capability."""
        result = area_manager.get_activity_tracking_areas()

        assert "living_room" in result
        assert result["living_room"] == "Living Room"
        assert "bedroom" in result
        assert result["bedroom"] == "Bedroom"
        assert "kitchen" not in result

    def test_get_light_automation_eligible_areas_requires_lights_and_presence(
        self, area_manager
    ):
        """Test that light automation requires both lights and presence detection."""
        result = area_manager.get_light_automation_eligible_areas()

        assert "living_room" in result
        assert "bedroom" in result
        assert "kitchen" not in result

    def test_get_all_areas_returns_all_monitored_areas(self, area_manager):
        """Test getting all areas with monitored entities."""
        result = area_manager.get_all_areas()

        assert "living_room" in result
        assert result["living_room"] == "Living Room"
        assert "bedroom" in result
        assert result["bedroom"] == "Bedroom"

    @pytest.mark.asyncio
    async def test_get_all_area_states_returns_list_of_area_data(
        self, area_manager, hass
    ):
        """Test getting state data for all areas."""

        def get_state(entity_id):
            states = {
                "binary_sensor.living_room_motion": State(
                    "binary_sensor.living_room_motion", "on"
                ),
                "binary_sensor.living_room_presence": State(
                    "binary_sensor.living_room_presence", "off"
                ),
                "sensor.living_room_illuminance": State(
                    "sensor.living_room_illuminance",
                    "50",
                    attributes={"device_class": "illuminance"},
                ),
                "media_player.living_room_tv": State(
                    "media_player.living_room_tv", "playing"
                ),
                "binary_sensor.bedroom_motion": State(
                    "binary_sensor.bedroom_motion", "off"
                ),
                "sensor.bedroom_temperature": State(
                    "sensor.bedroom_temperature",
                    "22",
                    attributes={"device_class": "temperature"},
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = await area_manager.get_all_area_states()

        assert len(result) >= 2
        area_ids = [area["area_id"] for area in result]
        assert "living_room" in area_ids
        assert "bedroom" in area_ids


class TestAreaManagerEntityQueries:
    """Test entity query methods."""

    def test_get_entity_area_returns_area_id(self, area_manager):
        """Test getting area ID for specific entity."""
        result = area_manager.get_entity_area("binary_sensor.living_room_motion")
        assert result == "living_room"

    def test_get_entity_area_returns_none_for_unknown_entity(self, area_manager):
        """Test that unknown entity returns None."""
        result = area_manager.get_entity_area("sensor.unknown")
        assert result is None

    def test_get_area_entities_returns_all_entities_in_area(self, area_manager):
        """Test getting all entities in an area."""
        result = area_manager.get_area_entities("living_room")

        assert "binary_sensor.living_room_motion" in result
        assert "binary_sensor.living_room_presence" in result
        assert "sensor.living_room_illuminance" in result
        assert "light.living_room" in result
        assert "media_player.living_room_tv" in result

    def test_get_area_entities_filters_by_domain(self, area_manager):
        """Test filtering entities by domain."""
        result = area_manager.get_area_entities("living_room", domain="binary_sensor")

        assert "binary_sensor.living_room_motion" in result
        assert "binary_sensor.living_room_presence" in result
        assert "sensor.living_room_illuminance" not in result
        assert "light.living_room" not in result

    def test_get_area_entities_filters_by_device_class(self, area_manager):
        """Test filtering entities by device class."""
        result = area_manager.get_area_entities(
            "living_room", domain="binary_sensor", device_class="motion"
        )

        assert "binary_sensor.living_room_motion" in result
        assert "binary_sensor.living_room_presence" not in result

    def test_get_tracking_entities_returns_monitored_entities(self, area_manager):
        """Test getting entities used for activity tracking."""
        result = area_manager.get_tracking_entities("living_room")

        assert "binary_sensor.living_room_motion" in result
        assert "binary_sensor.living_room_presence" in result
        assert "sensor.living_room_illuminance" in result
        assert "media_player.living_room_tv" in result
        assert "light.living_room" not in result


class TestAreaManagerBinaryPresence:
    """Test binary presence detection."""

    def test_compute_presence_detected_motion_on(self, area_manager):
        """Test presence detected with motion sensor on."""
        entity_states = {"motion": "on"}
        detected = area_manager._compute_presence_detected(entity_states)
        assert detected is True

    def test_compute_presence_detected_all_off(self, area_manager):
        """Test no presence when all sensors off."""
        entity_states = {"motion": "off", "presence": "off", "occupancy": "off"}
        detected = area_manager._compute_presence_detected(entity_states)
        assert detected is False

    def test_compute_presence_detected_any_sensor_triggers(self, area_manager):
        """Test that any active sensor triggers presence."""
        assert area_manager._compute_presence_detected({"presence": "on"}) is True
        assert area_manager._compute_presence_detected({"occupancy": "on"}) is True
        assert area_manager._compute_presence_detected({"media": "playing"}) is True
        assert area_manager._compute_presence_detected({"media": "on"}) is True

    def test_compute_presence_detected_media_off_state(self, area_manager):
        """Test media player off state doesn't trigger presence."""
        entity_states = {"media": "off"}
        detected = area_manager._compute_presence_detected(entity_states)
        assert detected is False

    @pytest.mark.asyncio
    async def test_get_area_state_returns_presence_detected(self, area_manager, hass):
        """Test that area state includes presence_detected boolean."""

        def get_state(entity_id):
            states = {
                "binary_sensor.living_room_motion": State(
                    "binary_sensor.living_room_motion",
                    "on",
                    attributes={"device_class": "motion"},
                ),
                "binary_sensor.living_room_presence": State(
                    "binary_sensor.living_room_presence",
                    "on",
                    attributes={"device_class": "presence"},
                ),
                "sensor.living_room_illuminance": State(
                    "sensor.living_room_illuminance",
                    "50",
                    attributes={"device_class": "illuminance"},
                ),
                "media_player.living_room_tv": State(
                    "media_player.living_room_tv", "playing"
                ),
            }
            return states.get(entity_id)

        hass.states.get = MagicMock(side_effect=get_state)

        result = await area_manager.get_area_state("living_room")

        assert result is not None
        assert "presence_detected" in result
        assert result["presence_detected"] is True
        assert "active_presence_entities" in result
        assert "binary_sensor.living_room_motion" in result["active_presence_entities"]
        assert "media_player.living_room_tv" in result["active_presence_entities"]
