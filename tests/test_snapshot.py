from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from touchstone.config import (
    ActionsConfig,
    Budget,
    Config,
    ConfigSource,
    EngineConfig,
    ExecutionConfig,
    ForgeConfig,
    GitConfig,
    LoopConfig,
)
from touchstone.config_v2 import GeneratedMetadata, TargetConfig, ValidationGateConfig
from touchstone.hosted.snapshot import compatibility, config_digest, snapshot_state
from touchstone.outcomes import RunOutcome, RunResult


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(
        state_dir=state,
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(slug="acme/widgets"),
        generated_metadata=SimpleNamespace(source_digest="profile-digest"),
    )


def test_snapshot_selects_only_allowlisted_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.state_dir / "events.jsonl").write_text("events", encoding="utf-8")
    (config.state_dir / "ledger.jsonl").write_text("ledger", encoding="utf-8")
    (config.state_dir / ".env").write_text("SECRET=value", encoding="utf-8")

    plan = snapshot_state(
        config,
        RunResult(RunOutcome.COMPLETED, candidate_id="candidate-1"),
        loop="code",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )

    assert set(plan.files) == {"events.jsonl", "ledger.jsonl"}
    assert plan.manifest.lineage == "candidate-1"
    assert ".env" not in plan.files


def test_snapshot_compatibility_returns_typed_clean_start_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan = snapshot_state(
        config,
        RunResult(RunOutcome.NO_CHANGE),
        loop="code",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )

    assert compatibility(plan.manifest, config, loop="code", lineage=plan.manifest.lineage).ok
    mismatch = compatibility(plan.manifest, config, loop="other", lineage=plan.manifest.lineage)
    assert mismatch.ok is False
    assert mismatch.clean_start_reason == "loop-mismatch"

    lineage_mismatch = compatibility(
        plan.manifest,
        config,
        loop="code",
        lineage="candidate-other",
    )
    assert lineage_mismatch.ok is False
    assert lineage_mismatch.clean_start_reason == "lineage-mismatch"


def test_config_digest_covers_effective_non_secret_runtime_configuration(
    tmp_path: Path,
) -> None:
    config = _complete_config(tmp_path)
    original = config_digest(config)

    loop = config.loops["code"]
    target = config.targets["app"]
    mutations = (
        replace(config, engine=replace(config.engine, model="gpt-5.6")),
        replace(config, actions=replace(config.actions, auto_merge=True)),
        replace(config, loops={"code": replace(loop, schedule="daily@03:00")}),
        replace(
            config,
            targets={
                "app": replace(
                    target,
                    validation=(replace(target.validation[0], enabled=False),),
                )
            },
        ),
    )

    assert all(config_digest(mutated) != original for mutated in mutations)


def _complete_config(tmp_path: Path) -> Config:
    return Config(
        source=ConfigSource(tmp_path / "touchstone.toml", 2),
        repo_path=tmp_path,
        state_dir=tmp_path / "state-complete",
        forge=ForgeConfig(slug="acme/widgets"),
        engine=EngineConfig(model="gpt-5.5", budget=Budget(audit=10, review=2)),
        execution=ExecutionConfig(),
        git=GitConfig(author_name="Touchstone", author_email="bot@example.com"),
        loops={
            "code": LoopConfig(
                name="code",
                brief="builtin:code-audit",
                label="touchstone:code",
                config_dir=tmp_path,
                schedule="hourly@00",
                targets=("app",),
            )
        },
        timezone="UTC",
        targets={
            "app": TargetConfig(
                id="app",
                path=Path("."),
                profiles=("python",),
                validation=(
                    ValidationGateConfig(
                        argv=("python", "-m", "pytest"),
                        timeout_seconds=300,
                        capability="source-read",
                        enabled=True,
                    ),
                ),
            )
        },
        generated_metadata=GeneratedMetadata(
            package_version="0.1.2",
            profile_versions=(("python", "1"),),
            source_digest="profile-digest",
            package_managers=("uv",),
        ),
        actions=ActionsConfig(action_sha="a" * 40),
    )
