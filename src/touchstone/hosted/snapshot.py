"""Allowlisted hosted state snapshots and strict restore compatibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
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
        config_digest=_config_digest(config),
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
    lineage: str,
) -> CompatibilityResult:
    expected = (
        (manifest.repository, config.forge.slug, "repository-mismatch"),
        (manifest.loop, loop, "loop-mismatch"),
        (manifest.schema_version, config.source.schema_version, "schema-mismatch"),
        (manifest.config_digest, _config_digest(config), "config-mismatch"),
        (
            manifest.profile_digest,
            config.generated_metadata.source_digest
            if getattr(config, "generated_metadata", None) is not None
            else "v1",
            "profile-mismatch",
        ),
        (manifest.lineage, lineage, "lineage-mismatch"),
    )
    for actual, wanted, reason in expected:
        if actual != wanted:
            return CompatibilityResult(False, reason)
    return CompatibilityResult(True)


def _config_digest(config: Any) -> str:
    safe = {
        "schema": config.source.schema_version,
        "repository": config.forge.slug,
        "profile": (
            config.generated_metadata.source_digest
            if getattr(config, "generated_metadata", None) is not None
            else "v1"
        ),
    }
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "CompatibilityResult",
    "SnapshotPlan",
    "compatibility",
    "snapshot_state",
]
