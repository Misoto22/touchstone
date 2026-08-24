"""Schema-v2 ownership boundary for project and generated configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from touchstone.config import Config, ConfigError, _build_config


@dataclass(frozen=True, slots=True)
class TargetConfig:
    id: str
    path: Path
    profiles: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    package_managers: tuple[str, ...] = ()
    validation: tuple[ValidationGateConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationGateConfig:
    argv: tuple[str, ...]
    timeout_seconds: int
    capability: str
    enabled: bool = False
    cwd: Path = Path(".")
    preparation: str = "none"
    shell: bool = False
    risk_acknowledged: bool = False
    allow_scripts: bool = False
    allow_build_hooks: bool = False


@dataclass(frozen=True, slots=True)
class GeneratedMetadata:
    package_version: str
    profile_versions: tuple[tuple[str, str], ...]
    source_digest: str
    package_managers: tuple[str, ...] = ()

    @property
    def package_manager(self) -> str:
        return self.package_managers[0] if self.package_managers else ""


def merge_generated(generated: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge generated values with project-owned overrides."""

    merged: dict[str, Any] = {}
    for key in generated.keys() | overrides.keys():
        if key not in overrides:
            merged[key] = generated[key]
            continue
        if key not in generated:
            merged[key] = overrides[key]
            continue
        base = generated[key]
        override = overrides[key]
        if isinstance(base, dict) and isinstance(override, dict):
            merged[key] = merge_generated(base, override)
        elif isinstance(base, list) and isinstance(override, list):
            merged[key] = _merge_lists(base, override)
        else:
            merged[key] = override
    return merged


def load_v2(root_path: Path, raw: dict[str, Any]) -> Config:
    root = root_path.expanduser().resolve()
    repository = root.parent.resolve()
    reference = raw.get("generated")
    if not isinstance(reference, str) or not reference.strip():
        raise ConfigError("schema v2 requires a non-empty generated path")
    relative = Path(reference)
    if relative.is_absolute():
        raise ConfigError("generated configuration must stay inside the repository")
    generated_path = (repository / relative).resolve()
    if not generated_path.is_relative_to(repository):
        raise ConfigError("generated configuration must stay inside the repository")
    try:
        generated = tomllib.loads(generated_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"generated configuration does not exist: {generated_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{generated_path} is not valid TOML: {exc}") from None

    merged = merge_generated(generated, raw)
    metadata = _metadata(merged.pop("metadata", {}))
    targets = _targets(merged.pop("target", {}), repository)
    merged.pop("generated", None)
    timezone = merged.pop("timezone", "UTC")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("timezone must be a non-empty IANA timezone string")
    merged["version"] = 2
    normalized = dict(merged)
    normalized["version"] = 1
    return _build_config(
        root,
        normalized,
        schema_version=2,
        generated_path=generated_path,
        timezone=timezone,
        targets=targets,
        generated_metadata=metadata,
    )


def _metadata(raw: object) -> GeneratedMetadata | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("metadata must be a table")
    package_version = raw.get("package_version")
    source_digest = raw.get("source_digest")
    versions = raw.get("profile_versions", {})
    managers = raw.get("package_managers", [])
    if not isinstance(package_version, str) or not package_version:
        raise ConfigError("metadata.package_version must be a non-empty string")
    if not isinstance(source_digest, str) or not source_digest:
        raise ConfigError("metadata.source_digest must be a non-empty string")
    if not isinstance(versions, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in versions.items()
    ):
        raise ConfigError("metadata.profile_versions must be a table of strings")
    if not _string_list(managers):
        raise ConfigError("metadata.package_managers must be an array of strings")
    return GeneratedMetadata(
        package_version=package_version,
        profile_versions=tuple(sorted(versions.items())),
        source_digest=source_digest,
        package_managers=tuple(managers),
    )


def _targets(raw: object, repository: Path) -> dict[str, TargetConfig]:
    if not isinstance(raw, dict):
        raise ConfigError("target must be a table")
    result: dict[str, TargetConfig] = {}
    for target_id, value in sorted(raw.items()):
        if not isinstance(value, dict):
            raise ConfigError(f"target.{target_id} must be a table")
        path_value = value.get("path")
        profiles = value.get("profiles", [])
        dependencies = value.get("dependencies", [])
        package_managers = value.get("package_managers", [])
        validation = value.get("validation", [])
        if not isinstance(path_value, str) or not path_value.strip():
            raise ConfigError(f"target.{target_id}.path must be a non-empty string")
        relative = Path(path_value)
        resolved = (repository / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(repository):
            raise ConfigError(f"target.{target_id}.path must stay inside the repository")
        if not _string_list(profiles):
            raise ConfigError(f"target.{target_id}.profiles must be an array of strings")
        if not _string_list(dependencies):
            raise ConfigError(f"target.{target_id}.dependencies must be an array of strings")
        if not _string_list(package_managers):
            raise ConfigError(f"target.{target_id}.package_managers must be an array of strings")
        result[target_id] = TargetConfig(
            id=target_id,
            path=relative,
            profiles=tuple(profiles),
            dependencies=tuple(dependencies),
            package_managers=tuple(package_managers),
            validation=_validation(validation, target_id),
        )
    return result


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _deduplicate(values: tuple[object, ...]) -> list[object]:
    result: list[object] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _merge_lists(base: list[object], override: list[object]) -> list[object]:
    """Merge keyed command records while retaining additive list semantics."""
    if not all(isinstance(item, dict) and isinstance(item.get("argv"), list) for item in base):
        return _deduplicate((*base, *override))
    result = [dict(item) for item in base]
    for value in override:
        if not isinstance(value, dict) or not isinstance(value.get("argv"), list):
            if value not in result:
                result.append(value)
            continue
        index = next(
            (
                position
                for position, current in enumerate(result)
                if current.get("argv") == value.get("argv")
            ),
            None,
        )
        if index is None:
            result.append(dict(value))
        else:
            result[index] = merge_generated(result[index], value)
    return result


def _validation(raw: object, target_id: str) -> tuple[ValidationGateConfig, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"target.{target_id}.validation must be an array of tables")
    allowed = {
        "argv",
        "timeout_seconds",
        "capability",
        "enabled",
        "cwd",
        "preparation",
        "shell",
        "risk_acknowledged",
        "allow_scripts",
        "allow_build_hooks",
    }
    result = []
    for index, value in enumerate(raw):
        where = f"target.{target_id}.validation[{index}]"
        if not isinstance(value, dict):
            raise ConfigError(f"{where} must be a table")
        extra = sorted(set(value) - allowed)
        if extra:
            raise ConfigError(f"unknown configuration key {where}.{extra[0]}")
        argv = value.get("argv")
        timeout = value.get("timeout_seconds", 300)
        capability = value.get("capability", "source-read")
        enabled = value.get("enabled", False)
        cwd = value.get("cwd", ".")
        if not _string_list(argv) or not argv:
            raise ConfigError(f"{where}.argv must be an array of non-empty strings")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError(f"{where}.timeout_seconds must be a positive integer")
        if not isinstance(capability, str) or not capability:
            raise ConfigError(f"{where}.capability must be a non-empty string")
        if not isinstance(enabled, bool) or not isinstance(cwd, str) or not cwd:
            raise ConfigError(f"{where} enabled/cwd values are invalid")
        relative_cwd = Path(cwd)
        if relative_cwd.is_absolute() or ".." in relative_cwd.parts:
            raise ConfigError(f"{where}.cwd must stay inside the Target")
        flags = {
            name: value.get(name, False)
            for name in (
                "shell",
                "risk_acknowledged",
                "allow_scripts",
                "allow_build_hooks",
            )
        }
        if any(not isinstance(flag, bool) for flag in flags.values()):
            raise ConfigError(f"{where} safety flags must be booleans")
        preparation = value.get("preparation", "none")
        if preparation not in {"none", "locked-install"}:
            raise ConfigError(f"{where}.preparation must be 'none' or 'locked-install'")
        result.append(
            ValidationGateConfig(
                argv=tuple(argv),
                timeout_seconds=timeout,
                capability=capability,
                enabled=enabled,
                cwd=relative_cwd,
                preparation=preparation,
                shell=flags["shell"],
                risk_acknowledged=flags["risk_acknowledged"],
                allow_scripts=flags["allow_scripts"],
                allow_build_hooks=flags["allow_build_hooks"],
            )
        )
    return tuple(result)


__all__ = [
    "GeneratedMetadata",
    "TargetConfig",
    "ValidationGateConfig",
    "load_v2",
    "merge_generated",
]
