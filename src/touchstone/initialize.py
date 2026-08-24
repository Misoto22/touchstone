"""Create project-owned and generated configuration from repository evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from touchstone.config import ConfigError, EngineName
from touchstone.discovery import ProjectDiscovery, discover_project
from touchstone.execution import Executor
from touchstone.profiles.materialize import (
    MaterializedConfig,
    detect_repository,
    materialize,
    select_package_managers,
)


@dataclass(frozen=True, slots=True)
class InitOptions:
    start: Path
    engine: EngineName
    model: str
    workflows: tuple[str, ...] = ()
    schedule: str = "hourly@00"
    timezone: str = "UTC"
    profiles: tuple[str, ...] = ()
    package_manager: str | None = None
    output: Path | None = None
    force: bool = False
    discovered: ProjectDiscovery | None = None


@dataclass(frozen=True, slots=True)
class InitReport:
    root: Path
    generated: Path
    materialized: MaterializedConfig


def initialize(options: InitOptions, executor: Executor) -> InitReport:
    if not options.model.strip():
        raise ConfigError("init requires an explicit model")
    if not options.timezone.strip():
        raise ConfigError("init requires a non-empty IANA timezone")
    found = options.discovered or discover_project(options.start, executor)
    target = (options.output or found.root / "touchstone.toml").expanduser().resolve()
    if target.parent != found.root.resolve():
        raise ConfigError("schema-v2 configuration must be written at the repository root")
    generated_path = found.root / ".touchstone/generated.toml"
    existing = [path for path in (target, generated_path) if path.exists()]
    if existing and not options.force:
        names = ", ".join(str(path) for path in existing)
        raise ConfigError(f"configuration already exists at {names}; pass --force to replace it")

    managers = select_package_managers(found.root, options.package_manager)
    discovery, matches, catalog = detect_repository(found.root, explicit_profiles=options.profiles)
    candidates = [
        match
        for target_matches in matches.values()
        for match in target_matches
        if match.verdict == "candidate"
    ]
    if candidates:
        choices = ", ".join(sorted({match.profile for match in candidates}))
        raise ConfigError(
            f"Profile candidates require confirmation ({choices}); pass --profile NAME"
        )
    generated = materialize(
        discovery,
        matches,
        catalog,
        package_managers=managers,
    )
    root_text = render_config(
        options,
        found,
        target_ids=tuple(target.id for target in discovery.targets),
    )
    _replace_pair(target, root_text, generated_path, generated.text)
    return InitReport(target, generated_path, generated)


def render_config(
    options: InitOptions,
    discovery: ProjectDiscovery,
    *,
    target_ids: tuple[str, ...] = (),
) -> str:
    root: dict[str, object] = {
        "version": 2,
        "generated": ".touchstone/generated.toml",
        "timezone": options.timezone,
        "project": {"path": "."},
        "forge": {
            "provider": "github",
            "slug": discovery.slug,
            "default_branch": discovery.default_branch,
            "required_workflows": list(options.workflows),
            "escalation_label": "touchstone:needs-review",
            "reap_after_hours": 6,
        },
        "engine": {
            "name": options.engine,
            "model": options.model,
            "audit_effort": "high",
            "review_effort": "high",
            "timeout_seconds": 2700,
        },
        "execution": {"target": "local"},
        "loop": {
            "code": {
                "brief": "builtin:code-audit",
                "label": "touchstone:audit",
                "schedule": options.schedule,
                "targets": list(target_ids),
            }
        },
    }
    if options.profiles:
        root["target"] = {
            target_id: {"profiles": list(options.profiles)} for target_id in target_ids
        }
    return (
        "# Project-owned Touchstone configuration. Generated stack evidence lives in\n"
        "# .touchstone/generated.toml and can be refreshed independently.\n\n" + tomli_w.dumps(root)
    )


def _replace_pair(root: Path, root_text: str, generated: Path, generated_text: str) -> None:
    root.parent.mkdir(parents=True, exist_ok=True)
    generated.parent.mkdir(parents=True, exist_ok=True)
    root_temporary = root.with_name(f".{root.name}.initializing")
    generated_temporary = generated.with_name(f".{generated.name}.initializing")
    originals = {
        root: root.read_bytes() if root.exists() else None,
        generated: generated.read_bytes() if generated.exists() else None,
    }
    try:
        root_temporary.write_text(root_text, encoding="utf-8")
        generated_temporary.write_text(generated_text, encoding="utf-8")
        generated_temporary.replace(generated)
        root_temporary.replace(root)
    except Exception:
        root_temporary.unlink(missing_ok=True)
        generated_temporary.unlink(missing_ok=True)
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise


def _quote(value: str) -> str:
    """Compatibility helper retained for integrations importing it."""
    return json.dumps(value, ensure_ascii=False)


__all__ = ["InitOptions", "InitReport", "initialize", "render_config"]
