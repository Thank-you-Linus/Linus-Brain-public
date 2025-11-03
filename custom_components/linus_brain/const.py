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

# Monitored domains and device classes
MONITORED_DOMAINS = {
    "binary_sensor": ["motion", "presence", "occupancy"],
    "sensor": ["illuminance"],
    "media_player": [],  # No device class
}

# Presence detection configuration
PRESENCE_DETECTION_DOMAINS = {
    "binary_sensor": ["motion", "presence", "occupancy"],
    "media_player": [],
}

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
    "none": {
        "conditions": [
            {
                "condition": "activity",
                "area_id": "current",
                "state": "none",
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
    "movement": {
        "activity_id": "movement",
        "activity_name": "Movement Detected",
        "description": "Short-term presence in area (motion detected)",
        "detection_conditions": [
            {
                "condition": "state",
                "domain": "binary_sensor",
                "device_class": "motion",
                "state": "on",
            }
        ],
        "duration_threshold_seconds": 0,
        "timeout_seconds": 0,
        "transition_to": "inactive",
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
    "occupied": {
        "activity_id": "occupied",
        "activity_name": "Occupied",
        "description": "Long-term presence in area (person staying)",
        "detection_conditions": [
            {
                "condition": "state",
                "domain": "binary_sensor",
                "device_class": "motion",
                "state": "on",
            }
        ],
        "duration_threshold_seconds": 60,
        "timeout_seconds": 0,
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
