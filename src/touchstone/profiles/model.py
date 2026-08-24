"""Immutable Profile catalog and evidence types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DetectionVerdict = Literal["confirmed", "candidate", "unsupported"]


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProfileMatch:
    profile: str
    verdict: DetectionVerdict
    evidence: tuple[Evidence, ...]
    detected_version: str | None = None
    warning: str = ""


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    id: str
    path: Path


@dataclass(frozen=True, slots=True)
class ValidationCandidate:
    argv: tuple[str, ...]
    timeout_seconds: int
    capability: str
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    name: str
    version: str
    category: str
    supported: str
    audit_context: str
    protected_paths: tuple[str, ...]
    source_paths: tuple[str, ...]
    detectors: tuple[tuple[tuple[str, str], ...], ...]
    validation: tuple[ValidationCandidate, ...]
    local: bool = False


@dataclass(frozen=True, slots=True)
class ProfileCatalog:
    profiles: Mapping[str, ProfileDefinition]

    def get(self, name: str) -> ProfileDefinition:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles))
            raise ValueError(f"unknown Profile {name!r}; known Profiles: {known}") from None
