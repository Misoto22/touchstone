"""Allowlisted hosted state snapshots and strict restore compatibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from touchstone.hosted.crypto import BundleManifest
from touchstone.outcomes import RunResult

_STATE_ALLOWLIST = (
    "events.jsonl",
    "ledger.jsonl",
    "checkpoints.sqlite",
    "due.sqlite",
)


@dataclass(frozen=True, slots=True)
class SnapshotPlan:
    manifest: BundleManifest
    files: dict[str, Path]


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    ok: bool
    clean_start_reason: str = ""


def snapshot_state(
    config: Any,
    run: RunResult,
    *,
    loop: str,
    run_id: str,
    created_at: str,
) -> SnapshotPlan:
    root = Path(config.state_dir).expanduser().resolve()
    files = {name: root / name for name in _STATE_ALLOWLIST if (root / name).is_file()}
    profile_digest = (
        config.generated_metadata.source_digest
        if getattr(config, "generated_metadata", None) is not None
        else "v1"
    )
    manifest = BundleManifest(
        repository=config.forge.slug,
        loop=loop,
        schema_version=config.source.schema_version,
        config_digest=config_digest(config),
        profile_digest=profile_digest,
        lineage=run.candidate_id or f"run:{run_id}",
        run_id=run_id,
        created_at=created_at,
        files=tuple(sorted(files)),
    )
    return SnapshotPlan(manifest, files)


def compatibility(
    manifest: BundleManifest,
    config: Any,
    *,
    loop: str,
    lineage: str | None,
) -> CompatibilityResult:
    expected = [
        (manifest.repository, config.forge.slug, "repository-mismatch"),
        (manifest.loop, loop, "loop-mismatch"),
        (manifest.schema_version, config.source.schema_version, "schema-mismatch"),
        (manifest.config_digest, config_digest(config), "config-mismatch"),
        (
            manifest.profile_digest,
            config.generated_metadata.source_digest
            if getattr(config, "generated_metadata", None) is not None
            else "v1",
            "profile-mismatch",
        ),
    ]
    if lineage is not None:
        expected.append((manifest.lineage, lineage, "lineage-mismatch"))
    for actual, wanted, reason in expected:
        if actual != wanted:
            return CompatibilityResult(False, reason)
    return CompatibilityResult(True)


def config_digest(config: Any) -> str:
    loops = {
        name: _loop_config(loop) for name, loop in sorted(getattr(config, "loops", {}).items())
    }
    execution = getattr(config, "execution", None)
    ssh = getattr(execution, "ssh", None)
    safe = {
        "schema": config.source.schema_version,
        "repository": config.forge.slug,
        "timezone": getattr(config, "timezone", "UTC"),
        "forge": getattr(config, "forge", {}),
        "engine": getattr(config, "engine", {}),
        "execution": {
            "target": getattr(execution, "target", "local"),
            "ssh": (
                {
                    "host": ssh.host,
                    "workdir": ssh.workdir,
                    "state_dir": ssh.state_dir,
                    "env_keys": sorted(key for key, _value in ssh.env),
                    "identity_file": ssh.identity_file,
                    "connect_timeout": ssh.connect_timeout,
                }
                if ssh is not None
                else None
            ),
        },
        "git": getattr(config, "git", {}),
        "actions": getattr(config, "actions", {}),
        "loops": loops,
        "targets": getattr(config, "targets", {}),
        "generated": getattr(config, "generated_metadata", None),
    }
    encoded = json.dumps(
        _canonical(safe),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if hasattr(value, "__dict__"):
        return _canonical(vars(value))
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Enum):
        return value.value
    return value


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _loop_config(loop: Any) -> dict[str, Any]:
    prompt = getattr(loop, "prompt", None)
    review_prompt = getattr(loop, "review_prompt", None)
    return {
        "name": getattr(loop, "name", ""),
        "brief": getattr(loop, "brief", ""),
        "brief_digest": _text_digest(prompt()) if callable(prompt) else "",
        "review_digest": _text_digest(review_prompt()) if callable(review_prompt) else "",
        "label": getattr(loop, "label", ""),
        "schedule": getattr(loop, "schedule", None),
        "priority": getattr(loop, "priority", 100),
        "protected_paths": getattr(loop, "protected_paths", ()),
        "require_change_under": getattr(loop, "require_change_under", ()),
        "confine_to": getattr(loop, "confine_to", ()),
        "targets": getattr(loop, "targets", ()),
        "context": getattr(loop, "context", ()),
    }


__all__ = [
    "CompatibilityResult",
    "SnapshotPlan",
    "compatibility",
    "config_digest",
    "snapshot_state",
]
