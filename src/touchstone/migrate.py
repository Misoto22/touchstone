"""Backup-first migration from the original unversioned TOML schema."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
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


def _migrate_v0(raw: dict[str, Any]) -> dict[str, Any]:
    if "repo_path" not in raw:
        raise ConfigError("unversioned configuration is missing repo_path")
    forge = dict(raw.get("forge", {}))
    forge.pop("audit_label", None)
    forge.pop("harness_label", None)
    forge.setdefault("provider", "github")
    forge.setdefault("required_workflows", ["verify-deploy.yml", "ci.yml"])

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


__all__ = ["MigrationReport", "migrate_config"]
