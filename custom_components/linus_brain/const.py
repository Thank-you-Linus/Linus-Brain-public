"""
Constants for Linus Brain integration.

This module contains all constant values used throughout the integration.
"""

# Integration domain
DOMAIN = "linus_brain"

# Configuration keys
CONF_SUPABASE_URL = "supabase_url"
CONF_SUPABASE_KEY = "supabase_key"
CONF_USE_SUN_ELEVATION = "use_sun_elevation"

# Activity types
ACTIVITY_EMPTY = "empty"

# Default rule template for light automation (fallback when no Supabase rules)
DEFAULT_ACTIVITY_RULES = {
    "presence": {
        "conditions": [
            {
                "condition": "activity",
                "area_id": "current",
                "state": "presence",
                "description": "Presence detected in area",
            },
            {
                "condition": "area_state",
                "area_id": "current",
                "attribute": "is_dark",
                "description": "Area is dark (lux < 20 OR sun < 3°)",
            },
        ],
        "actions": [
            {
                "service": "light.turn_on",
                "domain": "light",
                "area": "current",
                "description": "Turn on lights",
            }
        ],
        "description": "Turn on lights when presence detected AND area is dark",
    },
    "empty": {
        "conditions": [
            {
                "condition": "activity",
                "area_id": "current",
                "state": "empty",
                "description": "No presence detected in area",
            }
        ],
        "actions": [
            {
                "service": "light.turn_off",
                "domain": "light",
                "area": "current",
                "description": "Turn off lights",
            }
        ],
        "description": "Turn off lights when no presence detected",
    },
}

# Default activity types for dynamic activity detection system
DEFAULT_ACTIVITY_TYPES = {
    "empty": {
        "activity_id": "empty",
        "activity_name": "No Activity",
        "description": "No presence detected in area",
        "detection_conditions": [],
        "duration_threshold_seconds": 0,
        "timeout_seconds": 0,
        "transition_to": None,
        "is_transition_state": False,
        "is_system": True,
    },
    "inactive": {
        "activity_id": "inactive",
        "activity_name": "Inactive",
        "description": "Transition state after movement stops, before area becomes empty",
        "detection_conditions": [],
        "duration_threshold_seconds": 0,
        "timeout_seconds": 60,
        "transition_to": "empty",
        "is_transition_state": True,
        "is_system": True,
    },
    "movement": {
        "activity_id": "movement",
        "activity_name": "Movement Detected",
        "description": "Short-term presence in area (motion detected)",
        "detection_conditions": [
            {
                "condition": "or",
                "conditions": [
                    {
                        "condition": "state",
                        "domain": "binary_sensor",
                        "device_class": "motion",
                        "state": "on",
                    },
                    {
                        "condition": "state",
                        "domain": "media_player",
                        "state": "playing",
                    },
                ],
            }
        ],
        "duration_threshold_seconds": 0,
        "timeout_seconds": 1,
        "transition_to": "inactive",
        "is_transition_state": False,
        "is_system": True,
    },
    "occupied": {
        "activity_id": "occupied",
        "activity_name": "Occupied",
        "description": "Long-term presence in area (person staying or media playing)",
        "detection_conditions": [
            {
                "condition": "or",
                "conditions": [
                    {
                        "condition": "state",
                        "domain": "binary_sensor",
                        "device_class": "motion",
                        "state": "on",
                    },
                    {
                        "condition": "state",
                        "domain": "media_player",
                        "state": "playing",
                    },
                ],
            }
        ],
        "duration_threshold_seconds": 300,
        "timeout_seconds": 300,
        "transition_to": "inactive",
        "is_transition_state": False,
        "is_system": True,
    },
}

# Feature flags available for apps
AVAILABLE_FEATURES = {
    "automatic_lighting": {
        "app_id": "automatic_lighting",
        "name": "Automatic Lighting",
        "description": "Allume automatiquement les lumières en cas de mouvement",
        "default_enabled": False,  # OFF par défaut comme demandé
    }
}

# Default automatic_lighting app (ultimate fallback)
DEFAULT_AUTOLIGHT_APP = {
    "app_id": "automatic_lighting",
    "app_name": "Automatic Lighting",
    "description": "Turn lights on when movement detected in dark conditions, turn off when empty",
    "required_domains": ["light"],
    "recommended_sensors": ["motion", "illuminance"],
    "created_by": "system",
    "activity_actions": {
        "movement": {
            "activity_id": "movement",
            "conditions": [
                {
                    "condition": "area_state",
                    "area_id": "current",
                    "attribute": "is_dark",
                    "description": "Area is dark (lux < 20 OR sun < 3°)",
                }
            ],
            "actions": [
                {
                    "service": "light.turn_on",
                    "domain": "light",
                    "area": "current",
                    "data": {"brightness_pct": 100},
                    "description": "Turn on lights at full brightness",
                }
            ],
            "logic": "and",
            "description": "Turn on lights at full brightness when movement detected AND area is dark",
        },
        "inactive": {
            "activity_id": "inactive",
            "conditions": [
                {
                    "condition": "area_state",
                    "area_id": "current",
                    "attribute": "is_dark",
                    "description": "Area is dark (lux < 20 OR sun < 3°)",
                }
            ],
            "actions": [
                {
                    "service": "light.turn_on",
                    "domain": "light",
                    "area": "current",
                    "filter_entities_by_state": "on",
                    "data": {"brightness_step_pct": -10},
                    "description": "Dim lights by 10% (only lights that are ON)",
                }
            ],
            "logic": "and",
            "description": "Dim lights by 10% when area becomes inactive (only affects lights that are ON)",
        },
        "empty": {
            "activity_id": "empty",
            "conditions": [],
            "actions": [
                {
                    "service": "light.turn_off",
                    "domain": "light",
                    "area": "current",
                    "description": "Turn off lights",
                }
            ],
            "logic": "and",
            "description": "Turn off lights when area is empty",
        },
    },
}


# Monitored domains for entity discovery and tracking
# Maps domain to list of device_classes (empty list = monitor all entities in that domain)
MONITORED_DOMAINS = {
    "binary_sensor": ["motion"],  # Presence detection
    "media_player": [],  # Media player presence detection (all media players)
    "sensor": ["humidity", "illuminance", "temperature"],  # Environmental sensors for insights
}

# Presence detection domains for activity tracking
# Only includes domains/device_classes used for presence/movement detection
PRESENCE_DETECTION_DOMAINS = {
    "binary_sensor": ["motion"],  # Motion and presence sensors
    "media_player": [],  # Media players (playing state indicates presence)
}


# Insight sensor configuration
# Maps insight_type to HA sensor properties for display
# This enables future insight types without code changes
INSIGHT_SENSOR_CONFIG = {
    "dark_threshold_lux": {
        "device_class": "illuminance",  # SensorDeviceClass.ILLUMINANCE
        "unit": "lx",
        "icon": "mdi:brightness-5",
        "translation_key": "dark_threshold",
        "value_path": ["threshold"],  # Path in insight["value"] to extract number
    },
    "bright_threshold_lux": {
        "device_class": "illuminance",
        "unit": "lx",
        "icon": "mdi:brightness-7",
        "translation_key": "bright_threshold",
        "value_path": ["threshold"],
    },
    "default_brightness_pct": {
        "device_class": None,  # No standard device class for percentage
        "unit": "%",
        "icon": "mdi:brightness-percent",
        "translation_key": "default_brightness",
        "value_path": ["brightness"],
    },
    # Generic fallback for unknown insight types
    "_default": {
        "device_class": None,
        "unit": None,
        "icon": "mdi:information",
        "translation_key": "insight",
        "value_path": None,  # Will use entire value dict
    },
}

# Enabled insight types for sensor creation
# Currently only dark_threshold is enabled
# To enable more: add "bright_threshold_lux", "default_brightness_pct", etc.
ENABLED_INSIGHT_SENSORS = ["dark_threshold_lux"]


def get_insight_value(insight_data: dict, value_path: list[str] | None) -> any:
    """
    Extract value from insight data using path.
    
    Args:
        insight_data: Full insight dict with "value" key
        value_path: Path through nested dicts (e.g., ["threshold"])
    
    Returns:
        Extracted value or None
    
    Example:
        >>> insight = {"value": {"threshold": 20}, "confidence": 0.85}
        >>> get_insight_value(insight, ["threshold"])
        20
    """
    if value_path is None:
        return insight_data.get("value")
    
    current = insight_data.get("value", {})
    for key in value_path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


# Load manifest data at module import time (before async context)
# This avoids blocking I/O warnings when accessing manifest during entity setup
def _load_manifest_data() -> dict[str, str]:
    """
    Load manifest data from manifest.json file.
    
    This function is called once at module import time, before any async code runs.
    
    Returns:
        Dictionary with manifest fields like version, documentation_url, etc.
    """
    import json
    from pathlib import Path
    
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
            return {
                "version": manifest.get("version", "unknown"),
                "documentation": manifest.get("documentation", ""),
            }
    except Exception:
        return {
            "version": "unknown",
            "documentation": "https://github.com/Thank-you-Linus/Linus-Brain-public",
        }


# Load manifest once at module import (synchronous context, before async event loop)
_MANIFEST_DATA = _load_manifest_data()


def _get_manifest_data() -> dict[str, str]:
    """
    Get manifest data (loaded at import time).
    
    Returns:
        Dictionary with manifest fields like version, documentation_url, etc.
    """
    return _MANIFEST_DATA


def get_area_device_info(entry_id: str, area_id: str, area_name: str) -> dict:
    """
    Get device_info for an area-specific Linus Brain device.
    
    Creates a device per area that groups all Linus Brain entities for that area:
    - Area context sensor
    - Feature switches (automatic_lighting, etc.)
    
    Args:
        entry_id: Config entry ID
        area_id: Home Assistant area ID
        area_name: Human-readable area name
        
    Returns:
        Device info dictionary for device registry
    """
    manifest = _get_manifest_data()
    
    return {
        "identifiers": {(DOMAIN, f"{entry_id}_{area_id}")},
        "name": f"Linus Brain - {area_name}",
        "manufacturer": "Linus Brain",
        "model": "Area Intelligence",
        "sw_version": manifest["version"],
        # Note: suggested_area removed - devices are assigned via device_registry migration
        # This prevents creating duplicate areas when area_id is a hash
        "via_device": (DOMAIN, entry_id),  # Link to main integration device
        "configuration_url": f"{manifest['documentation']}/blob/master/docs/QUICKSTART.md",
    }


def get_integration_device_info(entry_id: str) -> dict:
    """
    Get device_info for the main Linus Brain integration device.
    
    This is the parent device that shows integration-wide sensors:
    - Last sync time
    - Cloud health
    - Total areas monitored
    
    Args:
        entry_id: Config entry ID
        
    Returns:
        Device info dictionary for device registry
    """
    from homeassistant.helpers.device_registry import DeviceEntryType
    
    manifest = _get_manifest_data()
    
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "Linus Brain",
        "manufacturer": "Linus Brain",
        "model": "Automation Engine",
        "sw_version": manifest["version"],
        "entry_type": DeviceEntryType.SERVICE,
        "configuration_url": f"{manifest['documentation']}/blob/master/docs/QUICKSTART.md",
    }
