"""
Feature Flag Manager for Linus Brain

Centralizes all feature flag logic to eliminate code duplication
and provide consistent behavior across system.

Key responsibilities:
- Manage feature flags per area (local storage)
- Provide feature flag checking for apps
- Handle persistence of feature states
- Create and manage feature switch entities
- Structured logging for debugging
- Metrics collection for monitoring
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .area_manager import AreaManager
from .app_storage import AppStorage

_LOGGER = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a validation operation."""

    is_valid: bool
    errors: list[str]
    warnings: list[str]
    suggestions: list[str]

    def has_issues(self) -> bool:
        """Check if there are any issues (errors or warnings)."""
        return bool(self.errors or self.warnings)

    def get_summary(self) -> str:
        """Get a human-readable summary of validation result."""
        if self.is_valid and not self.warnings:
            return "✅ Validation passed - No issues found"
        elif self.is_valid and self.warnings:
            return f"⚠️ Validation passed with {len(self.warnings)} warning(s)"
        else:
            return f"❌ Validation failed with {len(self.errors)} error(s)"


class FeatureFlagManager:
    """
    Centralizes feature flag logic for Linus Brain.

    This class provides a single source of truth for all feature flag
    operations, eliminating code duplication and ensuring consistent
    behavior across system.

    New architecture: Feature flags are stored locally and control
    specific apps per area, while activities remain always active.
    """

    def __init__(self, app_storage: AppStorage | None = None) -> None:
        """
        Initialize feature flag manager.

        Args:
            app_storage: AppStorage instance for persistence
        """
        self.app_storage = app_storage
        self._area_feature_states: dict[str, dict[str, bool]] = {}
        self._feature_definitions = {}
        self._metrics = {
            "total_checks": 0,
            "enabled_checks": 0,
            "disabled_checks": 0,
            "feature_evaluations": 0,
            "skipped_evaluations": 0,
        }
        self._last_reset = datetime.now()

        # Load feature definitions from const
        try:
            from ..const import AVAILABLE_FEATURES

            self._feature_definitions = AVAILABLE_FEATURES
        except ImportError:
            _LOGGER.warning("Could not load AVAILABLE_FEATURES from const")
            self._feature_definitions = {}

    async def load_feature_states(self, all_areas: list[str]) -> None:
        """
        Load feature states from AppStorage.

        Initializes with default values (False) if no data exists.

        Args:
            all_areas: List of all area IDs to initialize
        """
        if not self.app_storage:
            _LOGGER.warning("No AppStorage available, using empty feature states")
            self._area_feature_states = {}
            return

        try:
            data = await self.app_storage.async_load()
            feature_states = data.get("feature_states", {})

            # Initialize with default values (False) for all areas/features
            for area_id in all_areas:
                if area_id not in feature_states:
                    feature_states[area_id] = {}

                for feature_id in self._feature_definitions.keys():
                    feature_key = f"{feature_id}_enabled"
                    if feature_key not in feature_states[area_id]:
                        feature_states[area_id][feature_key] = False

            self._area_feature_states = feature_states
            _LOGGER.info(f"Loaded feature states for {len(feature_states)} areas")

        except Exception as err:
            _LOGGER.error(f"Failed to load feature states: {err}")
            self._area_feature_states = {}

    async def persist_feature_states(self) -> None:
        """
        Persist feature states to AppStorage.

        Saves immediately when changed.
        """
        if not self.app_storage:
            _LOGGER.warning("No AppStorage available, skipping persistence")
            return

        try:
            # AppStorage manages its own data, we need to update it directly
            data = await self.app_storage.async_load()
            data["feature_states"] = self._area_feature_states
            # Update the internal data in AppStorage
            self.app_storage._data = data
            await self.app_storage.async_save()
            _LOGGER.debug("Feature states persisted to AppStorage")

        except Exception as err:
            _LOGGER.error(f"Failed to persist feature states: {err}")

    def is_feature_enabled(self, area_id: str, feature_id: str) -> bool:
        """
        Check if a specific feature is enabled for an area.

        Args:
            area_id: The area ID to check
            feature_id: The feature ID to check

        Returns:
            True if feature is enabled, False otherwise
        """
        self._metrics["total_checks"] += 1
        self._metrics["feature_evaluations"] += 1

        feature_key = f"{feature_id}_enabled"
        is_enabled = self._area_feature_states.get(area_id, {}).get(feature_key, False)

        if is_enabled:
            self._metrics["enabled_checks"] += 1
            _LOGGER.debug(
                f"FeatureFlag: Feature {feature_id} ENABLED for area {area_id}"
            )
        else:
            self._metrics["disabled_checks"] += 1
            self._metrics["skipped_evaluations"] += 1
            _LOGGER.debug(
                f"FeatureFlag: Feature {feature_id} DISABLED for area {area_id}"
            )

        return is_enabled

    def set_feature_enabled(self, area_id: str, feature_id: str, enabled: bool) -> None:
        """
        Enable or disable a feature for an area.

        Args:
            area_id: The area ID
            feature_id: The feature ID
            enabled: Whether to enable feature
        """
        if area_id not in self._area_feature_states:
            self._area_feature_states[area_id] = {}

        feature_key = f"{feature_id}_enabled"
        old_value = self._area_feature_states[area_id].get(feature_key, False)

        if old_value != enabled:
            self._area_feature_states[area_id][feature_key] = enabled
            _LOGGER.info(
                f"FeatureFlag: Feature {feature_id} {'ENABLED' if enabled else 'DISABLED'} for area {area_id}"
            )

            # Persist immediately
            import asyncio

            asyncio.create_task(self.persist_feature_states())

    def get_feature_definitions(self) -> dict[str, dict[str, Any]]:
        """
        Get all available feature definitions.

        Returns:
            Dictionary of feature definitions
        """
        return self._feature_definitions.copy()

    def get_area_feature_states(self, area_id: str) -> dict[str, bool]:
        """
        Get all feature states for an area.

        Args:
            area_id: The area ID

        Returns:
            Dictionary of feature states for the area
        """
        return self._area_feature_states.get(area_id, {}).copy()

    def get_all_feature_states(self) -> dict[str, dict[str, bool]]:
        """
        Get all feature states for all areas.

        Returns:
            Dictionary of all feature states
        """
        return {
            area: states.copy() for area, states in self._area_feature_states.items()
        }

    def get_metrics(self) -> dict[str, Any]:
        """
        Get performance and usage metrics.

        Returns:
            Dictionary with metrics data
        """
        uptime = (datetime.now() - self._last_reset).total_seconds()

        return {
            "uptime_seconds": uptime,
            "total_checks": self._metrics["total_checks"],
            "enabled_checks": self._metrics["enabled_checks"],
            "disabled_checks": self._metrics["disabled_checks"],
            "feature_evaluations": self._metrics["feature_evaluations"],
            "skipped_evaluations": self._metrics["skipped_evaluations"],
            "enabled_areas_count": len(self._area_feature_states),
            "enabled_areas": list(self._area_feature_states.keys()),
            "check_rate": self._metrics["total_checks"] / max(uptime, 1),
            "skip_rate": self._metrics["skipped_evaluations"]
            / max(self._metrics["feature_evaluations"], 1),
        }

    def reset_metrics(self) -> None:
        """Reset all metrics counters."""
        self._metrics = {
            "total_checks": 0,
            "enabled_checks": 0,
            "disabled_checks": 0,
            "feature_evaluations": 0,
            "skipped_evaluations": 0,
        }
        self._last_reset = datetime.now()
        _LOGGER.info("FeatureFlagManager: Metrics reset")

    def log_feature_status(
        self, area_id: str, feature_id: str, context: str = ""
    ) -> None:
        """
        Log detailed status information for a feature.

        Args:
            area_id: The area ID
            feature_id: The feature ID
            context: Additional context for log
        """
        is_enabled = self.is_feature_enabled(area_id, feature_id)

        log_data = {
            "area_id": area_id,
            "feature_id": feature_id,
            "enabled": is_enabled,
            "context": context,
            "timestamp": datetime.now().isoformat(),
        }

        if is_enabled:
            _LOGGER.info(
                f"FeatureFlag: Feature {feature_id} ENABLED for area {area_id} - Context: {context}"
            )
        else:
            _LOGGER.info(
                f"FeatureFlag: Feature {feature_id} DISABLED for area {area_id} - Context: {context}"
            )

        _LOGGER.debug(f"FeatureFlag: Feature status details: {log_data}")

    def get_feature_status_explanation(
        self, area_id: str, feature_id: str
    ) -> dict[str, Any]:
        """
        Get a detailed explanation of why a feature is enabled/disabled.

        Args:
            area_id: The area ID
            feature_id: The feature ID

        Returns:
            Dictionary with detailed status explanation
        """
        is_enabled = self.is_feature_enabled(area_id, feature_id)
        feature_def = self._feature_definitions.get(feature_id, {})

        explanation = {
            "area_id": area_id,
            "feature_id": feature_id,
            "is_enabled": is_enabled,
            "feature_name": feature_def.get("name", feature_id),
            "feature_description": feature_def.get("description", ""),
            "default_enabled": feature_def.get("default_enabled", False),
            "last_check": datetime.now().isoformat(),
        }

        return explanation

    # ===== VALIDATION METHODS =====

    async def validate_feature_state(
        self, area_id: str, feature_id: str
    ) -> ValidationResult:
        """
        Validate feature configuration and state.

        Args:
            area_id: The area ID
            feature_id: The feature ID

        Returns:
            ValidationResult with detailed findings
        """
        errors: list[str] = []
        warnings: list[str] = []
        suggestions: list[str] = []

        # Check if feature exists
        if feature_id not in self._feature_definitions:
            errors.append(f"Feature '{feature_id}' is not defined")
            return ValidationResult(False, errors, warnings, suggestions)

        # Check feature state
        is_enabled = self.is_feature_enabled(area_id, feature_id)
        if not is_enabled:
            warnings.append(
                f"Feature '{feature_id}' is currently disabled for area '{area_id}'"
            )
            suggestions.append("Enable the feature to activate its functionality")

        # Check area exists
        if area_id not in self._area_feature_states:
            warnings.append(f"No feature states configured for area '{area_id}'")
            suggestions.append("Initialize feature states for this area")

        return ValidationResult(len(errors) == 0, errors, warnings, suggestions)

    # ===== DEBUGGING METHODS =====

    def get_system_overview(self) -> dict[str, Any]:
        """
        Get comprehensive system overview for debugging.

        Returns:
            Dictionary with system-wide debugging information
        """
        metrics = self.get_metrics()

        overview: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "system_health": self._assess_system_health(),
            "metrics": metrics,
            "feature_definitions": self._feature_definitions,
            "area_feature_states": self._area_feature_states,
            "issues": [],
            "recommendations": [],
        }

        # Identify system-wide issues
        overview["issues"] = self._identify_system_issues(overview)
        overview["recommendations"] = self._generate_system_recommendations(overview)

        return overview

    def _assess_system_health(self) -> dict[str, Any]:
        """Assess overall system health."""
        health: dict[str, Any] = {
            "overall_status": "healthy",
            "checks": {},
            "score": 100,
        }
        checks: dict[str, dict[str, str]] = {}
        health["checks"] = checks

        # Check feature definitions availability
        has_features = len(self._feature_definitions) > 0
        checks["feature_definitions"] = {
            "status": "pass" if has_features else "fail",
            "message": (
                f"{len(self._feature_definitions)} feature definitions available"
                if has_features
                else "No feature definitions available"
            ),
        }

        # Calculate overall score
        failed_checks = sum(
            1 for check in health["checks"].values() if check["status"] == "fail"
        )
        health["score"] = max(0, 100 - (failed_checks * 25))

        if health["score"] < 100:
            health["overall_status"] = "warning" if health["score"] >= 75 else "error"

        return health

    def _identify_system_issues(self, overview: dict[str, Any]) -> list[str]:
        """Identify system-wide issues from overview data."""
        issues = []
        metrics = overview["metrics"]

        if len(overview["feature_definitions"]) == 0:
            issues.append("No feature definitions available")

        if metrics["enabled_areas_count"] == 0:
            issues.append("No areas have feature states configured")

        if metrics["skip_rate"] > 0.8:
            issues.append("Very high skip rate for feature evaluations")

        return issues

    def _generate_system_recommendations(self, overview: dict[str, Any]) -> list[str]:
        """Generate system-wide recommendations."""
        recommendations = []
        metrics = overview["metrics"]

        if len(overview["feature_definitions"]) == 0:
            recommendations.append("Add feature definitions to enable app control")

        if metrics["enabled_areas_count"] == 0:
            recommendations.append(
                "Configure feature states for areas to enable app control"
            )

        if metrics["skip_rate"] > 0.5:
            recommendations.append(
                "Review disabled features - consider enabling if needed"
            )

        return recommendations

    def export_debug_data(self, format_type: str = "json") -> str:
        """
        Export debug data in specified format.

        Args:
            format_type: Export format ('json', 'csv', 'txt')

        Returns:
            Formatted debug data string
        """
        overview = self.get_system_overview()

        if format_type == "json":
            import json

            return json.dumps(overview, indent=2)

        elif format_type == "csv":
            lines = ["area_id,feature_id,enabled"]
            for area_id, features in overview["area_feature_states"].items():
                for feature_id, enabled in features.items():
                    lines.append(f"{area_id},{feature_id},{enabled}")
            return "\n".join(lines)

        elif format_type == "txt":
            lines = [
                f"Feature Flag Debug Report - {overview['timestamp']}",
                "=" * 50,
                f"System Health: {overview['system_health']['overall_status']} (Score: {overview['system_health']['score']})",
                f"Feature Definitions: {len(overview['feature_definitions'])}",
                f"Areas with States: {len(overview['area_feature_states'])}",
                "",
                "Feature Definitions:",
                "-" * 20,
            ]

            for feature_id, definition in overview["feature_definitions"].items():
                lines.extend(
                    [
                        f"Feature: {feature_id}",
                        f"  Name: {definition.get('name', 'Unknown')}",
                        f"  Description: {definition.get('description', 'No description')}",
                        f"  Default: {definition.get('default_enabled', False)}",
                        "",
                    ]
                )

            lines.extend(
                [
                    "Area Feature States:",
                    "-" * 25,
                ]
            )

            for area_id, features in overview["area_feature_states"].items():
                lines.append(f"Area: {area_id}")
                for feature_id, enabled in features.items():
                    lines.append(f"  {feature_id}: {'ON' if enabled else 'OFF'}")
                lines.append("")

            if overview["issues"]:
                lines.extend(
                    [
                        "Issues:",
                        "-" * 10,
                    ]
                    + overview["issues"]
                )

            if overview["recommendations"]:
                lines.extend(
                    [
                        "Recommendations:",
                        "-" * 20,
                    ]
                    + overview["recommendations"]
                )

            return "\n".join(lines)

        else:
            raise ValueError(f"Unsupported format: {format_type}")

    # ===== INITIALIZATION HELPERS =====

    def set_area_manager(self, area_manager: AreaManager) -> None:
        """Set area manager reference for validation."""
        self._area_manager = area_manager

    def set_app_storage(self, app_storage: AppStorage) -> None:
        """Set app storage reference for validation."""
        self.app_storage = app_storage
