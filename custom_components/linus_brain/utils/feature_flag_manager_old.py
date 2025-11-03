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
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .activity_tracker import ActivityTracker

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
            data = await self.app_storage.async_load()
            data["feature_states"] = self._area_feature_states
            await self.app_storage.async_save(data)
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
            enabled: Whether to enable the feature
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

    def filter_enabled_areas(self, area_ids: Iterable[str]) -> list[str]:
        """
        Filter a list to keep only enabled areas.

        Args:
            area_ids: Iterable of area IDs to filter

        Returns:
            List of enabled area IDs
        """
        if not self.rule_engine:
            _LOGGER.debug("FeatureFlag: No rule engine, no areas enabled")
            return []

        # Convert to list to get length for logging
        area_ids_list = list(area_ids)
        enabled = [
            area_id
            for area_id in area_ids_list
            if area_id in self.rule_engine._enabled_areas
        ]

        _LOGGER.debug(
            f"FeatureFlag: Filtered {len(area_ids_list)} areas, {len(enabled)} enabled: {enabled}"
        )
        return enabled

    async def evaluate_activity_if_enabled(
        self, area_id: str, activity_tracker: "ActivityTracker"
    ) -> str | None:
        """
        Evaluate activity only if the area is enabled.

        Args:
            area_id: The area ID
            activity_tracker: ActivityTracker instance

        Returns:
            Activity string if enabled, None otherwise
        """
        self._metrics["activity_evaluations"] += 1

        if not self.is_area_enabled(area_id):
            self._metrics["skipped_evaluations"] += 1
            _LOGGER.info(
                f"FeatureFlag: Skipping activity evaluation for disabled area {area_id}"
            )
            return None

        try:
            activity = await activity_tracker.async_evaluate_activity(area_id)
            _LOGGER.debug(
                f"FeatureFlag: Evaluated activity for enabled area {area_id}: {activity}"
            )
            return activity
        except Exception as err:
            _LOGGER.error(
                f"FeatureFlag: Failed to evaluate activity for {area_id}: {err}"
            )
            return None

    def should_process_area_update(self, area_id: str) -> bool:
        """
        Determine if an area update should be processed.

        This is a convenience method that combines the enabled check
        with additional validation logic.

        Args:
            area_id: The area ID to check

        Returns:
            True if update should be processed, False otherwise
        """
        if not self.is_area_enabled(area_id):
            return False

        # Additional validation can be added here
        # For example: check if area has required entities, etc.

        return True

    def get_enabled_areas_count(self) -> int:
        """
        Get the number of currently enabled areas.

        Returns:
            Number of enabled areas
        """
        if not self.rule_engine:
            return 0
        return len(self.rule_engine._enabled_areas)

    def get_enabled_areas_list(self) -> list[str]:
        """
        Get a list of currently enabled areas.

        Returns:
            List of enabled area IDs
        """
        if not self.rule_engine:
            return []
        return list(self.rule_engine._enabled_areas)

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
            "activity_evaluations": self._metrics["activity_evaluations"],
            "skipped_evaluations": self._metrics["skipped_evaluations"],
            "enabled_areas_count": self.get_enabled_areas_count(),
            "enabled_areas": self.get_enabled_areas_list(),
            "check_rate": self._metrics["total_checks"] / max(uptime, 1),
            "skip_rate": self._metrics["skipped_evaluations"]
            / max(self._metrics["activity_evaluations"], 1),
        }

    def reset_metrics(self) -> None:
        """Reset all metrics counters."""
        self._metrics = {
            "total_checks": 0,
            "enabled_checks": 0,
            "disabled_checks": 0,
            "activity_evaluations": 0,
            "skipped_evaluations": 0,
        }
        self._last_reset = datetime.now()
        _LOGGER.info("FeatureFlagManager: Metrics reset")

    def log_area_status(self, area_id: str, context: str = "") -> None:
        """
        Log detailed status information for an area.

        Args:
            area_id: The area ID to log
            context: Additional context for the log
        """
        is_enabled = self.is_area_enabled(area_id)

        log_data = {
            "area_id": area_id,
            "enabled": is_enabled,
            "context": context,
            "timestamp": datetime.now().isoformat(),
            "enabled_areas_count": self.get_enabled_areas_count(),
        }

        if is_enabled:
            _LOGGER.info(f"FeatureFlag: Area {area_id} ENABLED - Context: {context}")
        else:
            _LOGGER.info(f"FeatureFlag: Area {area_id} DISABLED - Context: {context}")

        _LOGGER.debug(f"FeatureFlag: Area status details: {log_data}")

    def get_area_status_explanation(self, area_id: str) -> dict[str, Any]:
        """
        Get a detailed explanation of why an area is enabled/disabled.

        Args:
            area_id: The area ID to analyze

        Returns:
            Dictionary with detailed status explanation
        """
        is_enabled = self.is_area_enabled(area_id)

        explanation = {
            "area_id": area_id,
            "is_enabled": is_enabled,
            "reason": self._get_enable_reason(area_id),
            "rule_engine_available": self.rule_engine is not None,
            "total_enabled_areas": self.get_enabled_areas_count(),
            "enabled_areas_list": self.get_enabled_areas_list(),
            "last_check": datetime.now().isoformat(),
        }

        if self.rule_engine and area_id in self.rule_engine._assignments:
            assignment = self.rule_engine._assignments[area_id]
            explanation.update(
                {
                    "has_assignment": True,
                    "assignment_app_id": assignment.get("app_id"),
                    "assignment_enabled": assignment.get("enabled", True),
                    "assignment_config": assignment.get("config", {}),
                }
            )
        else:
            explanation["has_assignment"] = False

        return explanation

    def _get_enable_reason(self, area_id: str) -> str:
        """
        Get the reason why an area is enabled or disabled.

        Args:
            area_id: The area ID to check

        Returns:
            String explanation of the enable/disable reason
        """
        if not self.rule_engine:
            return "No rule engine available"

        if area_id not in self.rule_engine._assignments:
            return "No assignment configured for this area"

        assignment = self.rule_engine._assignments[area_id]

        if not assignment.get("enabled", True):
            return "Assignment is explicitly disabled"

        if area_id in self.rule_engine._enabled_areas:
            return "Area is enabled in rule engine"

        return "Area is not enabled in rule engine (may be disabled by user or system)"

    # ===== VALIDATION METHODS =====

    async def validate_area_state(self, area_id: str) -> ValidationResult:
        """
        Validate area configuration and state.

        Args:
            area_id: The area ID to validate

        Returns:
            ValidationResult with detailed findings
        """
        errors: list[str] = []
        warnings: list[str] = []
        suggestions: list[str] = []

        # Check if area exists
        if hasattr(self, "_area_manager") and self._area_manager:
            area_state = await self._area_manager.get_area_state(area_id)
            if not area_state:
                errors.append(f"Area '{area_id}' does not exist or has no entities")
                return ValidationResult(False, errors, warnings, suggestions)

        # Check feature flag status
        is_enabled = self.is_area_enabled(area_id)
        if not is_enabled:
            warnings.append(f"Area '{area_id}' is currently disabled")
            suggestions.append(
                "Enable the area in configuration to activate automations"
            )

        # Check assignment consistency
        if self.rule_engine and area_id in self.rule_engine._assignments:
            assignment = self.rule_engine._assignments[area_id]
            if not assignment.get("enabled", True):
                warnings.append("Assignment exists but is disabled")
                suggestions.append("Enable the assignment or remove it completely")
        else:
            warnings.append("No assignment found for this area")
            suggestions.append("Create an assignment to enable automation features")

        return ValidationResult(len(errors) == 0, errors, warnings, suggestions)

    async def validate_system_state(self) -> dict[str, Any]:
        """
        Validate overall system state and configuration.

        Returns:
            Dictionary with validation results and recommendations
        """
        validation_results: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "overall_valid": True,
            "areas": {},
            "system_issues": [],
            "recommendations": [],
        }
        areas: dict[str, dict[str, Any]] = {}
        validation_results["areas"] = areas

        # Get all areas if area manager is available
        all_areas = []
        if hasattr(self, "_area_manager") and self._area_manager:
            all_areas = await self._area_manager.get_all_areas()  # type: ignore

        # Validate each area
        for area_id in all_areas:
            result = await self.validate_area_state(area_id)
            areas[area_id] = {
                "is_valid": result.is_valid,
                "errors": result.errors,
                "warnings": result.warnings,
                "suggestions": result.suggestions,
                "summary": result.get_summary(),
            }

            if not result.is_valid:
                validation_results["overall_valid"] = False

        # System-wide checks
        enabled_count = self.get_enabled_areas_count()
        if enabled_count == 0:
            system_issues: list[str] = validation_results["system_issues"]
            recommendations: list[str] = validation_results["recommendations"]
            system_issues.append("No areas are currently enabled")
            recommendations.append("Enable at least one area to activate automations")

        return validation_results

    # ===== DEBUGGING METHODS =====

    def get_system_overview(self) -> dict[str, Any]:
        """
        Get comprehensive system overview for debugging.

        Returns:
            Dictionary with system-wide debugging information
        """
        metrics = self.get_metrics()
        enabled_areas = self.get_enabled_areas_list()

        overview: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "system_health": self._assess_system_health(),
            "metrics": metrics,
            "enabled_areas": enabled_areas,
            "area_details": {},
            "issues": [],
            "recommendations": [],
        }

        # Add details for each area
        for area_id in enabled_areas:
            overview["area_details"][area_id] = self.get_area_status_explanation(
                area_id
            )

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

        # Check rule engine availability
        rule_engine_available = self.rule_engine is not None
        checks["rule_engine"] = {
            "status": "pass" if rule_engine_available else "fail",
            "message": (
                "Rule engine available"
                if rule_engine_available
                else "Rule engine not available"
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

        if metrics["enabled_areas_count"] == 0:
            issues.append("No areas are enabled")

        if metrics["skip_rate"] > 0.5:
            issues.append("High skip rate for activity evaluations")

        if metrics["check_rate"] > 10:
            issues.append("Very high check rate - possible performance issues")

        return issues

    def _generate_system_recommendations(self, overview: dict[str, Any]) -> list[str]:
        """Generate system-wide recommendations."""
        recommendations = []
        metrics = overview["metrics"]

        if metrics["enabled_areas_count"] == 0:
            recommendations.append(
                "Enable areas in configuration to activate automations"
            )

        if metrics["skip_rate"] > 0.3:
            recommendations.append(
                "Review disabled areas - consider enabling if needed"
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
            lines = ["area_id,is_enabled,has_assignment,reason"]
            for area_id, details in overview["area_details"].items():
                lines.append(
                    f"{area_id},{details['is_enabled']},{details.get('has_assignment', False)},\"{details['reason']}\""
                )
            return "\n".join(lines)

        elif format_type == "txt":
            lines = [
                f"Feature Flag Debug Report - {overview['timestamp']}",
                "=" * 50,
                f"System Health: {overview['system_health']['overall_status']} (Score: {overview['system_health']['score']})",
                f"Enabled Areas: {len(overview['enabled_areas'])}",
                "",
                "Area Details:",
                "-" * 20,
            ]

            for area_id, details in overview["area_details"].items():
                lines.extend(
                    [
                        f"Area: {area_id}",
                        f"  Enabled: {details['is_enabled']}",
                        f"  Reason: {details['reason']}",
                        "",
                    ]
                )

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
        self._app_storage = app_storage
