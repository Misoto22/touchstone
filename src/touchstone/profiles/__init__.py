"""Explainable, non-executing repository stack detection."""

from touchstone.profiles.catalog import load_catalog
from touchstone.profiles.detect import detect_profiles
from touchstone.profiles.model import (
    DetectionVerdict,
    Evidence,
    ProfileCatalog,
    ProfileDefinition,
    ProfileMatch,
    TargetCandidate,
    ValidationCandidate,
)
from touchstone.profiles.targets import (
    ProjectTarget,
    TargetDiscovery,
    affected_targets,
    discover_targets,
)

__all__ = [
    "DetectionVerdict",
    "Evidence",
    "ProfileCatalog",
    "ProfileDefinition",
    "ProfileMatch",
    "ProjectTarget",
    "TargetCandidate",
    "TargetDiscovery",
    "ValidationCandidate",
    "affected_targets",
    "detect_profiles",
    "discover_targets",
    "load_catalog",
]
