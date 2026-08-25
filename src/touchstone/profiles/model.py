"""Immutable Profile catalog and evidence types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DetectionVerdict = Literal["confirmed", "candidate", "unsupported"]

#: Capabilities a Profile may enable on its own. A side-effect-minimal Gate
#: reads repository state with a tool Touchstone already requires and runs no
#: project-provided code, so enabling it needs no operator review. Everything
#: else materializes as a disabled Validation Candidate.
MINIMAL_CAPABILITIES = frozenset({"source-read"})


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
    #: Naming rules this stack holds itself to, as `(what, convention)` pairs.
    #:
    #: Declared as data rather than written into a brief, so one concern brief
    #: serves every stack and `doctor` can compare a rule against the project
    #: instead of only a session reading prose about it.
    naming: tuple[tuple[str, str], ...] = ()
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
