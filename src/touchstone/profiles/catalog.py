"""Load packaged and repository-local declarative Profiles."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any

from touchstone.profiles.model import (
    MINIMAL_CAPABILITIES,
    ProfileCatalog,
    ProfileDefinition,
    ValidationCandidate,
)

_ALLOWED = {
    "name",
    "version",
    "category",
    "supported",
    "audit_context",
    "protected_paths",
    "source_paths",
    "detect",
    "validation",
}
_DETECT_KEYS = {"kind", "path", "name", "ecosystem"}
_SAFE_DETECTORS = {"file", "dependency", "python-project", "node-engine"}
_VALIDATION_KEYS = {"argv", "timeout_seconds", "capability", "enabled"}
_BUILTIN_ORDER = (
    "generic",
    "javascript",
    "node",
    "typescript",
    "react",
    "nextjs",
    "python",
    "fastapi",
    "django",
)


def load_catalog(local_dir: Path | None = None) -> ProfileCatalog:
    packaged = files("touchstone.resources").joinpath("profiles")
    definitions: dict[str, ProfileDefinition] = {}
    for item in sorted(packaged.iterdir(), key=lambda entry: entry.name):
        if item.name.endswith(".toml") and item.is_file():
            definition = _parse(item.read_text(encoding="utf-8"), local=False)
            definitions[definition.name] = definition
    definitions = {name: definitions[name] for name in _BUILTIN_ORDER if name in definitions} | {
        name: value for name, value in definitions.items() if name not in _BUILTIN_ORDER
    }
    if local_dir is not None and local_dir.exists():
        for path in sorted(local_dir.glob("*.toml")):
            definition = _parse(path.read_text(encoding="utf-8"), local=True)
            definitions[definition.name] = definition
    return ProfileCatalog(definitions)


def _parse(text: str, *, local: bool) -> ProfileDefinition:
    raw = tomllib.loads(text)
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise ValueError(f"Profile contains unsupported key {unknown[0]!r}")
    name = _required_string(raw, "name")
    version = _required_string(raw, "version")
    category = _required_string(raw, "category")
    supported = str(raw.get("supported", ""))
    audit_context = str(raw.get("audit_context", ""))
    protected = _strings(raw.get("protected_paths", []), "protected_paths")
    sources = _strings(raw.get("source_paths", []), "source_paths")
    detectors: list[tuple[tuple[str, str], ...]] = []
    for detector in _tables(raw.get("detect", []), "detect"):
        extra = sorted(set(detector) - _DETECT_KEYS)
        if extra:
            raise ValueError(f"Profile detector contains unsupported key {extra[0]!r}")
        kind = _required_string(detector, "kind")
        if kind not in _SAFE_DETECTORS:
            raise ValueError(f"Profile detector kind {kind!r} is not declarative")
        detectors.append(tuple(sorted((key, str(value)) for key, value in detector.items())))
    validation: list[ValidationCandidate] = []
    for candidate in _tables(raw.get("validation", []), "validation"):
        extra = sorted(set(candidate) - _VALIDATION_KEYS)
        if extra:
            raise ValueError(f"Profile validation contains unsupported key {extra[0]!r}")
        argv = _strings(candidate.get("argv", []), "validation.argv")
        timeout = candidate.get("timeout_seconds", 300)
        capability = candidate.get("capability", "source-read")
        enabled = candidate.get("enabled", False)
        if not argv or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("Profile validation requires argv and a positive timeout")
        if not isinstance(capability, str) or not isinstance(enabled, bool):
            raise ValueError("Profile validation capability/enabled values are invalid")
        if enabled and capability not in MINIMAL_CAPABILITIES:
            raise ValueError(
                f"Profile validation cannot enable capability {capability!r}; only "
                "side-effect-minimal Gates are enabled without operator review"
            )
        validation.append(ValidationCandidate(argv, timeout, capability, enabled))
    return ProfileDefinition(
        name=name,
        version=version,
        category=category,
        supported=supported,
        audit_context=audit_context,
        protected_paths=protected,
        source_paths=sources,
        detectors=tuple(detectors),
        validation=tuple(validation),
        local=local,
    )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Profile {key} must be a non-empty string")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Profile {where} must be an array of strings")
    return tuple(value)


def _tables(value: object, where: str) -> Iterable[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Profile {where} must be an array of tables")
    return value


__all__ = ["load_catalog"]
