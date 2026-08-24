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

__all__ = [
    "DetectionVerdict",
    "Evidence",
    "ProfileCatalog",
    "ProfileDefinition",
    "ProfileMatch",
    "TargetCandidate",
    "ValidationCandidate",
    "detect_profiles",
    "load_catalog",
]
