"""Backup-first migration from the original unversioned TOML schema."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import tomli_w

from touchstone.config import ConfigError


@dataclass(frozen=True, slots=True)
class MigrationReport:
    path: Path
    backup: Path
    from_version: int
    to_version: int


@dataclass(frozen=True, slots=True)
class MigrationPreview:
    path: Path
    generated_path: Path
    backup: Path
    root_text: str
    generated_text: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V2MigrationReport:
    path: Path
    generated: Path
    backup: Path
    from_version: int = 1
    to_version: int = 2


def migrate_config(path: Path) -> MigrationReport:
    source = path.expanduser().resolve()
    try:
        original = source.read_bytes()
        raw = tomllib.loads(original.decode("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {source}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{source} is not valid UTF-8 TOML: {exc}") from None
    if "version" in raw:
        raise ConfigError(f"{source} is already versioned")

    migrated = _migrate_v0(raw)
    backup = _available_backup(source)
    backup.write_bytes(original)
    temporary = source.with_name(f".{source.name}.migrating")
    try:
        temporary.write_text(tomli_w.dumps(migrated), encoding="utf-8")
        temporary.replace(source)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return MigrationReport(source, backup, 0, 1)


def preview_v2_migration(path: Path, *, timezone: str, hourly_minute: int) -> MigrationPreview:
    source = path.expanduser().resolve()
    if not timezone.strip():
        raise ConfigError("timezone must be a non-empty IANA timezone string")
    if hourly_minute < 0 or hourly_minute > 59:
        raise ConfigError("hourly minute must be between 0 and 59")
    try:
        original = source.read_bytes()
        raw = tomllib.loads(original.decode("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {source}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{source} is not valid UTF-8 TOML: {exc}") from None
    if raw.get("version") != 1:
        raise ConfigError("v2 migration requires a version 1 configuration")

    migrated = dict(raw)
    migrated["version"] = 2
    migrated["generated"] = ".touchstone/generated.toml"
    migrated["timezone"] = timezone
    warnings: list[str] = []
    loops = {name: dict(table) for name, table in dict(raw.get("loop", {})).items()}
    for name, table in loops.items():
        if table.get("schedule") == "hourly":
            table["schedule"] = f"hourly@{hourly_minute:02d}"
            warnings.append(
                f"loop.{name}.schedule changed from hourly to hourly@{hourly_minute:02d}"
            )
    migrated["loop"] = loops

    generated = {
        "metadata": {
            "package_version": _package_version(),
            "profile_versions": {"generic": "1"},
            "source_digest": f"sha256:{hashlib.sha256(original).hexdigest()}",
        },
        "target": {
            "repository": {
                "path": ".",
                "profiles": ["generic"],
                "dependencies": [],
            }
        },
    }
    generated_path = source.parent / ".touchstone/generated.toml"
    if generated_path.exists():
        raise ConfigError(
            f"v2 generated configuration already exists: {generated_path}; move it before migration"
        )
    return MigrationPreview(
        path=source,
        generated_path=generated_path,
        backup=_available_version_backup(source, 1),
        root_text=tomli_w.dumps(migrated),
        generated_text=tomli_w.dumps(generated),
        warnings=tuple(warnings),
    )


def apply_v2_migration(preview: MigrationPreview) -> V2MigrationReport:
    try:
        original = preview.path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"no configuration at {preview.path}") from None
    preview.backup.write_bytes(original)
    preview.generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_temporary = preview.generated_path.with_name(
        f".{preview.generated_path.name}.migrating"
    )
    root_temporary = preview.path.with_name(f".{preview.path.name}.migrating")
    try:
        generated_temporary.write_text(preview.generated_text, encoding="utf-8")
        root_temporary.write_text(preview.root_text, encoding="utf-8")
        generated_temporary.replace(preview.generated_path)
        root_temporary.replace(preview.path)
    except Exception:
        generated_temporary.unlink(missing_ok=True)
        root_temporary.unlink(missing_ok=True)
        raise
    return V2MigrationReport(
        path=preview.path,
        generated=preview.generated_path,
        backup=preview.backup,
    )


def _migrate_v0(raw: dict[str, Any]) -> dict[str, Any]:
    if "repo_path" not in raw:
        raise ConfigError("unversioned configuration is missing repo_path")
    forge = dict(raw.get("forge", {}))
    forge.pop("audit_label", None)
    forge.pop("harness_label", None)
    forge.setdefault("provider", "github")
    # A migration cannot infer the target repository's workflow names. An
    # empty list is visible to doctor and blocks live publication until the
    # operator configures the real default-branch guarantees.
    forge.setdefault("required_workflows", [])

    loops: dict[str, Any] = {}
    builtins = {
        "briefs/code-audit.md": "builtin:code-audit",
        "briefs/harness-review.md": "builtin:harness-review",
    }
    for name, table in dict(raw.get("loop", {})).items():
        migrated = dict(table)
        reference = migrated.get("brief")
        if reference in builtins:
            migrated["brief"] = builtins[reference]
        if migrated.get("brief") == "builtin:harness-review":
            # v1 read this off `require_change_under`, which the harness review
            # was alone in setting. Recording it keeps that loop's "never more
            # than one open at a time" across the migration.
            migrated.setdefault("drafts_hold_slot", True)
        loops[name] = migrated

    result: dict[str, Any] = {
        "version": 1,
        "project": {"path": raw["repo_path"]},
    }
    if "state_dir" in raw:
        result["state_dir"] = raw["state_dir"]
    result["forge"] = forge
    for table in ("engine", "execution", "git"):
        if table in raw:
            result[table] = raw[table]
    result["loop"] = loops
    return result


def _available_backup(path: Path) -> Path:
    first = path.with_name(f"{path.name}.v0.bak")
    if not first.exists():
        return first
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}.v0.{counter}.bak")
        if not candidate.exists():
            return candidate
        counter += 1


def _available_version_backup(path: Path, source_version: int) -> Path:
    first = path.with_name(f"{path.name}.v{source_version}.bak")
    if not first.exists():
        return first
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}.v{source_version}.{counter}.bak")
        if not candidate.exists():
            return candidate
        counter += 1


def _package_version() -> str:
    try:
        return version("touchstone-agent")
    except PackageNotFoundError:
        return "0+unknown"


__all__ = [
    "MigrationPreview",
    "MigrationReport",
    "V2MigrationReport",
    "apply_v2_migration",
    "migrate_config",
    "preview_v2_migration",
]
