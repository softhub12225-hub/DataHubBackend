"""Feature gates.

Only flags with a present purpose are defined. The set is intentionally tiny: a flag
that nothing reads is dead configuration, and a flag per unbuilt feature is how a
codebase acquires dozens of untested branches.

``rankings`` exists now, ahead of any ranking code, because architecture decision D8
requires ranking ingestion, display and filtering to be disabled as a unit until an
authorised source is licensed. Establishing the gate before the feature guarantees
there is no path that ships rankings ungated.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    """Resolved feature state for the running process."""

    rankings: bool
    """D8: ranking ingestion, display and filtering. Off until a licence exists."""

    readiness_checks_object_storage: bool
    """Whether /health/ready treats the evidence store as a required dependency."""

    @classmethod
    def from_settings(cls, settings: Settings) -> FeatureFlags:
        return cls(
            rankings=settings.rankings_enabled,
            readiness_checks_object_storage=settings.readiness_check_object_storage,
        )


def get_feature_flags(settings: Settings) -> FeatureFlags:
    return FeatureFlags.from_settings(settings)
