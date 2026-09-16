"""
Pytest configuration and fixtures for Linus Brain tests.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..const import CONF_SUPABASE_KEY, CONF_SUPABASE_URL, DOMAIN

# Import pytest-homeassistant-custom-component plugin fixtures
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
def mock_hass():
    """Mock Home Assistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {DOMAIN: {}}
    hass.config.language = "en"
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


@pytest.fixture
def mock_hass_fr():
    """Mock Home Assistant instance with French language."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {DOMAIN: {}}
    hass.config.language = "fr"
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


@pytest.fixture
def mock_config_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        CONF_SUPABASE_URL: "https://test.supabase.co",
        CONF_SUPABASE_KEY: "test_key",
    }
    return entry


@pytest.fixture
def mock_area_registry():
    """Mock area registry."""
    registry = MagicMock(spec=ar.AreaRegistry)
    registry.async_get_area = MagicMock(return_value=None)
    registry.async_list_areas = MagicMock(return_value=[])
    return registry


@pytest.fixture
def mock_entity_registry():
    """Mock entity registry."""
    registry = MagicMock(spec=er.EntityRegistry)
    registry.entities = MagicMock()
    registry.entities.get_entries_for_area = MagicMock(return_value=[])
    return registry


@pytest.fixture
def mock_device_registry():
    """Mock device registry."""
    registry = MagicMock(spec=dr.DeviceRegistry)
    registry.devices = MagicMock()
    return registry


# ---------------------------------------------------------------------------
# Entity filtering contract (see area_manager.py "CRITICAL PATTERN")
# ---------------------------------------------------------------------------


class MockStateMachine:
    """Stand-in for ``hass.states`` that actually stores ``State`` objects.

    A ``MagicMock`` state machine returns ``None`` (or an auto-``MagicMock``)
    for every lookup, which silently violates rule 3 of the "CRITICAL PATTERN -
    Entity Filtering" contract documented at the top of
    ``custom_components/linus_brain/utils/area_manager.py``: production code
    skips every registry entry whose ``hass.states.get(entity_id)`` is ``None``.
    A fixture that never registers a state therefore filters out *all* entities,
    and the tests built on it pass or fail for the wrong reason.

    This stub keeps a real ``dict[str, State]`` so entities can be registered
    incrementally and looked up exactly like the real state machine. Assign it
    to ``hass.states`` and register one state per mocked registry entry.
    """

    def __init__(self) -> None:
        """Initialize an empty state machine."""
        self._states: dict[str, State] = {}

    def register(
        self,
        entity_id: str,
        state: str = "off",
        attributes: dict[str, Any] | None = None,
    ) -> State:
        """Register (or replace) the state of an entity and return it."""
        state_obj = State(entity_id, state, attributes or {})
        self._states[entity_id] = state_obj
        return state_obj

    def get(self, entity_id: str) -> State | None:
        """Return the state registered for ``entity_id``, or ``None``."""
        return self._states.get(entity_id)

    def remove(self, entity_id: str) -> bool:
        """Unregister an entity so that ``get`` returns ``None`` again."""
        return self._states.pop(entity_id, None) is not None

    def async_all(self, domain_filter: str | None = None) -> list[State]:
        """Return every registered state, optionally filtered by domain."""
        if domain_filter is None:
            return list(self._states.values())
        prefix = f"{domain_filter}."
        return [
            state
            for entity_id, state in self._states.items()
            if entity_id.startswith(prefix)
        ]


def build_mock_registry_entry(
    entity_id: str,
    *,
    area_id: str | None = None,
    device_id: str | None = None,
    original_device_class: str | None = None,
    device_class: str | None = None,
    platform: str = "test",
    disabled_by: Any = None,
    hidden_by: Any = None,
) -> MagicMock:
    """Build a mock ``er.RegistryEntry`` that honours the entity-filtering contract.

    ``utils/area_manager.py`` documents, under "CRITICAL PATTERN - Entity
    Filtering", that every iteration over ``entity_registry.entities`` must skip:

    1. Linus Brain's own entities (``entity.platform == DOMAIN``)
    2. Disabled entities (``entity.disabled_by is not None``)
    3. Entities without state (``hass.states.get(entity_id) is None``)

    ``utils/entity_resolver.py`` applies rules 2 and 3 identically. A
    ``MagicMock(spec=er.RegistryEntry)`` that leaves ``disabled_by`` unset still
    *answers* the attribute access — with an auto-``MagicMock``, which is not
    ``None``, so rule 2 discards the entity. Any mocked entry must therefore set
    ``disabled_by`` explicitly, and ``platform`` to something other than
    ``linus_brain`` so rule 1 is exercised rather than accidentally satisfied.

    Do not build registry entries by hand in a test module: use this factory (or
    the ``mock_registry_entry_factory`` fixture, which also registers the state
    rule 3 requires) so the contract cannot drift again.

    Args:
        entity_id: Full entity id; its prefix also provides ``domain``.
        area_id: Direct area assignment, or ``None`` to rely on the device.
        device_id: Owning device, used for the area fallback.
        original_device_class: Device class as set at entity creation.
        device_class: Current device class; mirrors ``original_device_class``
            when omitted, like Home Assistant does for untouched entities.
        platform: Owning integration. Defaults to ``"test"`` — never ``DOMAIN``.
        disabled_by: Disable reason. Defaults to ``None`` (entity enabled).
        hidden_by: Hide reason. Defaults to ``None``. No production code reads
            this attribute today; it is set for forward-compatibility only.

    Returns:
        A ``MagicMock`` specced on ``er.RegistryEntry``.
    """
    entry = MagicMock(spec=er.RegistryEntry)
    entry.entity_id = entity_id
    entry.domain = entity_id.split(".")[0]
    entry.area_id = area_id
    entry.device_id = device_id
    entry.original_device_class = original_device_class
    entry.device_class = original_device_class if device_class is None else device_class
    entry.platform = platform
    entry.disabled_by = disabled_by
    entry.hidden_by = hidden_by
    return entry


@pytest.fixture
def mock_states() -> MockStateMachine:
    """Provide a state machine stub to assign to ``hass.states``."""
    return MockStateMachine()


@pytest.fixture
def mock_registry_entry_factory(
    mock_states: MockStateMachine,
) -> Callable[..., MagicMock]:
    """Return a factory building a registry entry *and* registering its state.

    This is the single entry point for mocked entities: it wraps
    ``build_mock_registry_entry`` (rules 1 and 2 of the entity-filtering
    contract) and registers the entity in the ``mock_states`` stub (rule 3), so
    an entity created through it survives production's filters instead of being
    silently discarded.

    The returned callable accepts every keyword of
    ``build_mock_registry_entry`` plus:

    - ``state``: state string to register (default ``"off"``).
    - ``state_attributes``: extra state attributes. ``device_class`` is added
      automatically from the entry when not provided.
    - ``register_state``: set to ``False`` to build an entry with *no* state, to
      exercise the "entity without state" branch on purpose.
    """

    def _factory(
        entity_id: str,
        *,
        state: str = "off",
        state_attributes: dict[str, Any] | None = None,
        register_state: bool = True,
        **kwargs: Any,
    ) -> MagicMock:
        entry = build_mock_registry_entry(entity_id, **kwargs)
        if register_state:
            attributes = dict(state_attributes or {})
            if entry.device_class is not None:
                attributes.setdefault("device_class", entry.device_class)
            mock_states.register(entity_id, state, attributes)
        return entry

    return _factory
