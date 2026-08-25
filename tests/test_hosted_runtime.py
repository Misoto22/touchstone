from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.config import ConfigError
from touchstone.execution.base import Result
from touchstone.hosted.crypto import BundleManifest, encrypt_bundle
from touchstone.hosted.runtime import (
    CandidateIntegrityError,
    CandidateMetadata,
    HostedOutputs,
    VerificationAttestation,
    run_stage,
    validate_stage_environment,
    verify_candidate,
)
from touchstone.outcomes import ChangeState, RunOutcome, RunResult
from touchstone.scheduling.store import DueStore


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        repo_path=tmp_path,
        state_dir=tmp_path / ".touchstone" / "state",
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(slug="acme/widgets", default_branch="main"),
        execution=SimpleNamespace(target="local"),
        loops={},
        timezone="UTC",
        generated_metadata=SimpleNamespace(source_digest="profile-digest"),
    )


def _key() -> bytes:
    return bytes(range(32))


def test_stage_environment_keeps_model_and_publish_credentials_apart() -> None:
    validate_stage_environment("analysis", {"OPENAI_API_KEY": "model"})
    validate_stage_environment("install", {})
    validate_stage_environment(
        "verify", {"GH_TOKEN": "read-only-token", "TOUCHSTONE_STATE_KEY": "state"}
    )
    validate_stage_environment("publish", {"GH_TOKEN": "app-token"})

    with pytest.raises(CandidateIntegrityError, match="publishing credential"):
        validate_stage_environment(
            "analysis",
            {"OPENAI_API_KEY": "model", "TOUCHSTONE_APP_PRIVATE_KEY": "pem"},
        )
    with pytest.raises(CandidateIntegrityError, match="model credential"):
        validate_stage_environment("publish", {"GH_TOKEN": "app-token", "OPENAI_API_KEY": "model"})
    with pytest.raises(CandidateIntegrityError, match="model credential"):
        validate_stage_environment("verify", {"OPENAI_API_KEY": "model"})
    with pytest.raises(CandidateIntegrityError, match="publishing credential"):
        validate_stage_environment("verify", {"TOUCHSTONE_APP_PRIVATE_KEY": "pem"})
    with pytest.raises(CandidateIntegrityError, match="credential"):
        validate_stage_environment("snapshot", {"GH_TOKEN": "app-token"})
    with pytest.raises(CandidateIntegrityError, match="credential"):
        validate_stage_environment("install", {"OPENAI_API_KEY": "model"})


def test_agent_runtime_install_uses_an_isolated_secret_free_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from touchstone.hosted import runtime

    observed: dict[str, object] = {"calls": []}

    def which(command: str) -> str | None:
        return "/usr/bin/npm" if command == "npm" else None

    def install(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls = observed["calls"]
        assert isinstance(calls, list)
        calls.append(argv)
        observed["environment"] = kwargs["env"]
        observed["cwd"] = kwargs["cwd"]
        prefix = Path(kwargs["cwd"])
        if argv[1] == "ci":
            binary = prefix / "node_modules" / ".bin" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.149.1\n", stderr="")

    monkeypatch.setattr(runtime.shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", install)
    config = SimpleNamespace(
        engine=SimpleNamespace(name="codex"),
        actions=SimpleNamespace(),
        execution=SimpleNamespace(target="local"),
    )

    runtime._ensure_engine(
        config,
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_ACTION_PATH": str(Path(__file__).resolve().parents[1]),
            "RUNNER_TEMP": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/runner",
            "OPENAI_API_KEY": "model-secret",
            "TOUCHSTONE_STATE_KEY": "state-secret",
        },
        allow_install=True,
    )

    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["HOME"].startswith(str(tmp_path))
    assert "OPENAI_API_KEY" not in environment
    assert "TOUCHSTONE_STATE_KEY" not in environment
    calls = observed["calls"]
    assert isinstance(calls, list)
    argv = calls[0]
    assert argv[1] == "ci"
    assert "--ignore-scripts" in argv
    assert "--include=optional" in argv
    assert "--prefix" not in argv
    assert "@openai/codex@0.149.1" not in argv
    assert Path(observed["cwd"]).is_relative_to(tmp_path)  # type: ignore[arg-type]
    assert calls[1][-1] == "--version"


def test_agent_runtime_install_rejects_an_unusable_optional_platform_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from touchstone.hosted import runtime

    def which(command: str) -> str | None:
        return "/usr/bin/npm" if command == "npm" else None

    def install(argv, **kwargs):  # type: ignore[no-untyped-def]
        prefix = Path(kwargs["cwd"])
        if argv[1] == "ci":
            binary = prefix / "node_modules" / ".bin" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="missing platform package")

    monkeypatch.setattr(runtime.shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", install)
    config = SimpleNamespace(
        engine=SimpleNamespace(name="codex"),
        actions=SimpleNamespace(),
    )

    with pytest.raises(ConfigError, match=r"could not execute codex CLI 0\.149\.1"):
        runtime._ensure_engine(
            config,
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_ACTION_PATH": str(Path(__file__).resolve().parents[1]),
                "RUNNER_TEMP": str(tmp_path),
            },
            allow_install=True,
        )


def test_claude_runtime_runs_only_its_locked_postinstall_before_smoke_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from touchstone.hosted import runtime

    calls: list[list[str]] = []

    def which(command: str) -> str | None:
        return {"npm": "/usr/bin/npm", "node": "/usr/bin/node"}.get(command)

    def install(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        prefix = Path(kwargs["cwd"])
        if argv[1] == "ci":
            binary = prefix / "node_modules" / ".bin" / "claude"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            postinstall = prefix / "node_modules" / "@anthropic-ai" / "claude-code" / "install.cjs"
            postinstall.parent.mkdir(parents=True)
            postinstall.write_text("// locked postinstall\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "/usr/bin/node":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="2.1.241 (Claude Code)\n", stderr="")

    monkeypatch.setattr(runtime.shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", install)
    config = SimpleNamespace(
        engine=SimpleNamespace(name="claude"),
        actions=SimpleNamespace(),
    )

    runtime._ensure_engine(
        config,
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_ACTION_PATH": str(Path(__file__).resolve().parents[1]),
            "RUNNER_TEMP": str(tmp_path),
        },
        allow_install=True,
    )

    assert calls[0][1] == "ci"
    assert calls[1][0] == "/usr/bin/node"
    assert calls[1][1].endswith("/@anthropic-ai/claude-code/install.cjs")
    assert calls[2][-1] == "--version"


def test_agent_runtime_version_comes_from_the_committed_lock() -> None:
    from touchstone.hosted import runtime

    action_path = Path(__file__).resolve().parents[1]
    version = runtime._locked_agent_version(
        "codex",
        "@openai/codex",
        action_path / "agent-runtime" / "codex" / "package.json",
        action_path / "agent-runtime" / "codex" / "package-lock.json",
    )

    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)


def test_agent_runtime_rejects_a_manifest_that_disagrees_with_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime

    runtime_source = tmp_path / "action" / "agent-runtime" / "codex"
    runtime_source.mkdir(parents=True)
    (runtime_source / "package.json").write_text(
        json.dumps({"dependencies": {"@openai/codex": "0.149.2"}}), encoding="utf-8"
    )
    (runtime_source / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"dependencies": {"@openai/codex": "0.149.1"}},
                    "node_modules/@openai/codex": {"version": "0.149.1"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda command: "/usr/bin/npm" if command == "npm" else None,
    )
    config = SimpleNamespace(engine=SimpleNamespace(name="codex"), actions=SimpleNamespace())

    with pytest.raises(ConfigError, match="disagree"):
        runtime._ensure_engine(
            config,
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_ACTION_PATH": str(tmp_path / "action"),
                "RUNNER_TEMP": str(tmp_path / "runner"),
            },
            allow_install=True,
        )


def test_agent_runtime_rejects_a_lock_whose_resolved_version_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime

    runtime_source = tmp_path / "action" / "agent-runtime" / "codex"
    runtime_source.mkdir(parents=True)
    (runtime_source / "package.json").write_text(
        json.dumps({"dependencies": {"@openai/codex": "0.149.1"}}), encoding="utf-8"
    )
    (runtime_source / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"dependencies": {"@openai/codex": "0.149.1"}},
                    "node_modules/@openai/codex": {"version": "0.149.3"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda command: "/usr/bin/npm" if command == "npm" else None,
    )
    config = SimpleNamespace(engine=SimpleNamespace(name="codex"), actions=SimpleNamespace())

    with pytest.raises(ConfigError, match="disagree"):
        runtime._ensure_engine(
            config,
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_ACTION_PATH": str(tmp_path / "action"),
                "RUNNER_TEMP": str(tmp_path / "runner"),
            },
            allow_install=True,
        )


def test_analysis_never_installs_a_missing_agent_runtime_with_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from touchstone.hosted import runtime

    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda command: "/usr/bin/npm" if command == "npm" else None,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("analysis must not install packages"),
    )
    config = SimpleNamespace(
        engine=SimpleNamespace(name="codex"),
        actions=SimpleNamespace(),
    )

    with pytest.raises(ConfigError, match="secret-free install step"):
        runtime._ensure_engine(
            config,
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_ACTION_PATH": str(Path(__file__).resolve().parents[1]),
                "RUNNER_TEMP": str(tmp_path),
                "OPENAI_API_KEY": "model-secret",
            },
        )


def test_hosted_outputs_are_versioned_and_write_github_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_output = tmp_path / "github-output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    output = HostedOutputs(
        stage="analysis",
        run_id="run-1",
        outcome="proposed",
        loop="code",
        candidate_id="candidate-1",
        should_run=True,
    )

    output.write(tmp_path / "result.json")

    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["loop"] == "code"
    assert payload["candidate_id"] == "candidate-1"
    assert "loop=code" in github_output.read_text(encoding="utf-8")
    assert "candidate_id=candidate-1" in github_output.read_text(encoding="utf-8")
    assert "Analysis" in summary.read_text(encoding="utf-8")


def test_verification_attestation_binds_candidate_patch_and_effective_config(
    tmp_path: Path,
) -> None:
    attestation = VerificationAttestation(
        candidate_id="candidate-1",
        run_id="run-1",
        base_sha="a" * 40,
        patch_digest="sha256:" + "b" * 64,
        config_digest="sha256:" + "c" * 64,
    )
    path = tmp_path / "verified.json"

    attestation.write(path)

    assert VerificationAttestation.read(path) == attestation
    assert "worktree" not in json.loads(path.read_text(encoding="utf-8"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["patch_digest"] = "not-a-digest"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CandidateIntegrityError, match="attestation"):
        VerificationAttestation.read(path)


def test_publish_verifies_candidate_before_any_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    patch = tmp_path / "candidate.patch"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    metadata = CandidateMetadata(
        repository="acme/widgets",
        loop="code",
        finding_id="finding-1",
        candidate_id="candidate-1",
        run_id="run-1",
        base_sha="a" * 40,
        patch_digest=f"sha256:{hashlib.sha256(patch.read_bytes()).hexdigest()}",
        branch="touchstone/candidate-1",
        finding={"title": "Finding", "commit_subject": "fix: finding"},
        risk="low",
        verdict="approve",
        verdict_reason="covered",
    )
    metadata_path = tmp_path / "candidate.json"
    metadata_path.write_text(metadata.to_json(), encoding="utf-8")
    from touchstone.hosted.snapshot import config_digest

    manifest = BundleManifest(
        repository="acme/widgets",
        loop="code",
        schema_version=2,
        config_digest=config_digest(config),
        profile_digest="profile-digest",
        lineage="candidate-1",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )
    bundle = encrypt_bundle(
        manifest,
        {"candidate.json": metadata_path, "candidate.patch": patch},
        _key(),
    )
    bundle_path = tmp_path / "candidate.bundle.json"
    bundle_path.write_text(bundle.to_json(), encoding="utf-8")

    with pytest.raises(CandidateIntegrityError, match="loop-mismatch"):
        verify_candidate(
            config,
            bundle_path,
            key=_key(),
            destination=tmp_path / "wrong-loop",
            expected_base="a" * 40,
            expected_loop="other",
            expected_lineage="candidate-1",
        )
    with pytest.raises(CandidateIntegrityError, match="lineage-mismatch"):
        verify_candidate(
            config,
            bundle_path,
            key=_key(),
            destination=tmp_path / "wrong-lineage",
            expected_base="a" * 40,
            expected_loop="code",
            expected_lineage="candidate-other",
        )
    # Authentication still succeeds: this simulates a producer bug or a
    # malicious producer with the state key, so the independent digest check matters.
    bad = json.loads(metadata_path.read_text(encoding="utf-8"))
    bad["patch_digest"] = "sha256:" + "0" * 64
    metadata_path.write_text(json.dumps(bad), encoding="utf-8")
    bundle_path.write_text(
        encrypt_bundle(
            manifest,
            {"candidate.json": metadata_path, "candidate.patch": patch},
            _key(),
        ).to_json(),
        encoding="utf-8",
    )

    with pytest.raises(CandidateIntegrityError, match="patch digest"):
        verify_candidate(
            config,
            bundle_path,
            key=_key(),
            destination=tmp_path / "restored",
            expected_base="a" * 40,
            expected_loop="code",
            expected_lineage="candidate-1",
        )


def test_state_key_never_appears_in_output_contract(tmp_path: Path) -> None:
    encoded = base64.urlsafe_b64encode(_key()).decode()
    output = HostedOutputs(stage="snapshot", run_id="run-1", outcome="completed")
    target = tmp_path / "result.json"
    output.write(target, env={"TOUCHSTONE_STATE_KEY": encoded})

    assert encoded not in target.read_text(encoding="utf-8")


def test_no_due_analysis_and_snapshot_complete_without_model_or_publish_credentials(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    encoded = base64.urlsafe_b64encode(_key()).decode()
    env = {
        "TOUCHSTONE_STATE_KEY": encoded,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }

    analysis = run_stage(config, "analysis", env=env)
    snapshot = run_stage(config, "snapshot", env=env)

    assert analysis.outcome == "no_change"
    assert analysis.reason_code == "not-due"
    assert snapshot.outcome == "completed"
    assert (tmp_path / ".touchstone" / "hosted" / "snapshot" / "state.bundle.json").is_file()


def test_snapshot_falls_back_to_prepare_state_when_analysis_artifact_is_absent(
    tmp_path: Path,
) -> None:
    from touchstone.hosted import runtime

    config = _config(tmp_path)
    ledger = Path(config.state_dir) / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"event":"preserved"}\n', encoding="utf-8")
    prepared = tmp_path / ".touchstone" / "hosted" / "inputs" / "prepare"
    prepared.mkdir(parents=True)
    runtime._write_state_bundle(
        config,
        prepared / "state.bundle.json",
        key=_key(),
        run_id="prior-run",
        result=RunResult(RunOutcome.COMPLETED),
    )
    ledger.unlink()

    snapshot = run_stage(
        config,
        "snapshot",
        env={
            "TOUCHSTONE_STATE_KEY": base64.urlsafe_b64encode(_key()).decode(),
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
        },
    )

    assert snapshot.outcome == "completed"
    assert snapshot.clean_start_reason == ""
    assert ledger.read_text(encoding="utf-8") == '{"event":"preserved"}\n'


def test_hosted_due_slot_is_finalized_only_by_durable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    loop = SimpleNamespace(
        name="code",
        schedule="hourly@00",
        priority=10,
        targets=(),
    )
    config.loops = {"code": loop}
    config.loop = lambda name: config.loops[name]
    config.engine = SimpleNamespace(name="codex")
    encoded = base64.urlsafe_b64encode(_key()).decode()
    now = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)
    env = {
        "TOUCHSTONE_STATE_KEY": encoded,
        "TOUCHSTONE_NOW": now.isoformat(),
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    monkeypatch.setattr("touchstone.hosted.runtime._ensure_engine", lambda *_args: None)
    monkeypatch.setattr(
        "touchstone.hosted.runtime._analyze_loop",
        lambda *_args, **_kwargs: (RunResult(RunOutcome.NO_CHANGE), None),
    )

    analysis = run_stage(config, "analysis", env=env)

    store = DueStore(Path(config.state_dir) / "due.sqlite")
    pending = store.records()[0]
    assert analysis.outcome == "no_change"
    assert pending.consumed_at is None
    assert pending.claim_owner == "github:12345-1"

    run_stage(config, "snapshot", env=env)

    finalized = store.record(pending.slot_id)
    assert finalized is not None
    assert finalized.consumed_at == now
    assert finalized.claim_owner == ""


def test_proposed_due_slot_finalizes_from_candidate_artifact_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    loop = SimpleNamespace(
        name="code",
        schedule="hourly@00",
        priority=10,
        targets=(),
    )
    config.loops = {"code": loop}
    config.loop = lambda name: config.loops[name]
    config.engine = SimpleNamespace(name="codex")
    encoded = base64.urlsafe_b64encode(_key()).decode()
    now = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)
    env = {
        "TOUCHSTONE_STATE_KEY": encoded,
        "TOUCHSTONE_NOW": now.isoformat(),
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    candidate = CandidateMetadata(
        repository="acme/widgets",
        loop="code",
        finding_id="finding-1",
        candidate_id="candidate-1",
        run_id="12345-1",
        base_sha="a" * 40,
        patch_digest="sha256:" + "b" * 64,
        branch="touchstone/finding-1-candidate-1",
        finding={"title": "Finding", "commit_subject": "fix: finding"},
        risk="high",
        verdict="skipped",
        verdict_reason="operator review required",
    )
    monkeypatch.setattr("touchstone.hosted.runtime._ensure_engine", lambda *_args: None)
    monkeypatch.setattr(
        "touchstone.hosted.runtime._analyze_loop",
        lambda *_args, **_kwargs: (
            RunResult(
                RunOutcome.COMPLETED,
                lifecycle=ChangeState.PROPOSED,
                candidate_id=candidate.candidate_id,
            ),
            candidate,
        ),
    )

    analysis = run_stage(config, "analysis", env=env)
    hosted = tmp_path / ".touchstone" / "hosted"
    artifact_input = hosted / "inputs" / "candidate"
    artifact_input.parent.mkdir(parents=True)
    (hosted / "candidate").rename(artifact_input)

    snapshot = run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_OUTCOME": "completed",
            "TOUCHSTONE_FINAL_CANDIDATE_ID": candidate.candidate_id,
            "TOUCHSTONE_FINAL_CHANGE_STATE": "awaiting_human",
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "success",
        },
    )

    record = DueStore(Path(config.state_dir) / "due.sqlite").records()[0]
    assert analysis.outcome == "proposed"
    assert snapshot.outcome == "completed"
    assert record.consumed_at == now
    assert record.claim_owner == ""


def test_reanalyze_without_a_fresh_candidate_closes_the_old_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime

    projection = SimpleNamespace(
        finding_id="candidate-old",
        pr=12,
        head_sha="a" * 40,
        loop="code",
    )
    context = SimpleNamespace(
        ledger=SimpleNamespace(projection=lambda _identifier: projection),
        forge=object(),
        executor=object(),
    )
    resumed: list[str] = []

    class Lifecycle:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def resume(self, request):  # type: ignore[no-untyped-def]
            resumed.append(request.decision)
            return SimpleNamespace(outcome="reanalyze", pr=12)

    config = _config(tmp_path)
    config.forge.reap_after_hours = 6
    monkeypatch.setattr("touchstone.nodes.context.configure", lambda _config: context)
    monkeypatch.setattr("touchstone.lifecycle.RepositoryLifecycle", Lifecycle)
    monkeypatch.setattr(runtime, "_restore_state_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_write_state_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "verify_candidate",
        lambda *_args, **_kwargs: pytest.fail("no candidate should be verified"),
    )

    output = runtime._publish_stage(
        config,
        tmp_path / ".touchstone" / "hosted",
        "run-1",
        {
            "TOUCHSTONE_STATE_KEY": base64.urlsafe_b64encode(_key()).decode(),
            "TOUCHSTONE_CANDIDATE_ID": "candidate-old",
            "TOUCHSTONE_DECISION": "reanalyze",
        },
    )

    assert resumed == ["reanalyze"]
    assert output.outcome == "completed"
    assert output.change_state == "closed"


def test_approve_resume_blocks_when_exact_parked_head_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime

    worktree = tmp_path / "resume-worktree"
    worktree.mkdir()
    projection = SimpleNamespace(
        finding_id="candidate-old",
        pr=12,
        head_sha="a" * 40,
        branch="touchstone/candidate-old",
        loop="code",
    )
    context = SimpleNamespace(
        forge=SimpleNamespace(
            pull=lambda _number: SimpleNamespace(
                head_sha="a" * 40,
                branch="touchstone/candidate-old",
                closed=False,
                merged_at=None,
            )
        ),
        executor=SimpleNamespace(run=lambda *_args, **_kwargs: Result(0, "", "")),
    )
    config = _config(tmp_path)
    config.loop = lambda _name: SimpleNamespace(targets=("root",))
    monkeypatch.setattr(runtime, "_checkout_resume_worktree", lambda *_args: worktree)
    monkeypatch.setattr(runtime, "_remove_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("touchstone.runner._health_gate", lambda *_args: None)
    monkeypatch.setattr("touchstone.runner._publication_gate", lambda *_args: None)
    monkeypatch.setattr(
        "touchstone.validation.validate",
        lambda *_args, **_kwargs: SimpleNamespace(blocked=True),
    )

    with pytest.raises(CandidateIntegrityError, match="parked candidate failed"):
        runtime._validate_resume_candidate(
            config,
            projection,
            context,
            {"GH_TOKEN": "read-token", "TOUCHSTONE_STATE_KEY": "state-secret"},
        )


def test_resume_download_queries_the_exact_candidate_artifact_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime

    metadata = tmp_path / "candidate.json"
    patch = tmp_path / "candidate.patch"
    metadata.write_text("{}", encoding="utf-8")
    patch.write_text("", encoding="utf-8")
    bundle = encrypt_bundle(
        BundleManifest(
            repository="acme/widgets",
            loop="code",
            schema_version=2,
            config_digest="sha256:test",
            profile_digest="profile-digest",
            lineage="candidate-exact",
            run_id="run-1",
            created_at="2026-08-24T12:00:00Z",
        ),
        {"candidate.json": metadata, "candidate.patch": patch},
        _key(),
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("candidate.bundle.json", bundle.to_json())
    requested: list[str] = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def read(self, *_args):  # type: ignore[no-untyped-def]
            return self.payload

    def open_url(request, **_kwargs):  # type: ignore[no-untyped-def]
        requested.append(request.full_url)
        if "actions/artifacts" in request.full_url:
            payload = {
                "total_count": 250,
                "artifacts": [
                    {
                        "name": "touchstone-candidate-candidate-exact",
                        "expired": False,
                        "created_at": "2026-08-24T12:00:00Z",
                        "archive_download_url": "https://api.github.test/artifacts/250/zip",
                    }
                ],
            }
            return Response(json.dumps(payload).encode())
        return Response(archive.getvalue())

    monkeypatch.setattr(runtime.urllib.request, "urlopen", open_url)
    # The archive itself is fetched through an opener that drops the API token
    # on GitHub's redirect to signed storage, so it does not pass through urlopen.
    monkeypatch.setattr(
        runtime,
        "_open_artifact_archive",
        lambda url, headers, timeout: open_url(urllib.request.Request(url)),
    )
    destination = tmp_path / "restored.bundle.json"

    reason = runtime._download_artifact_file(
        _config(tmp_path),
        {"GH_TOKEN": "read-token", "GITHUB_REPOSITORY": "acme/widgets"},
        artifact_prefix="touchstone-candidate-",
        artifact_name="touchstone-candidate-candidate-exact",
        member="candidate.bundle.json",
        destination=destination,
        lineage="candidate-exact",
    )

    assert reason == ""
    assert destination.is_file()
    assert "name=touchstone-candidate-candidate-exact" in requested[0]


def test_verify_does_not_require_push_scoped_repository_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify runs with repository read, so it must not consult the Publication Gate.

    A read-scoped token makes GitHub omit ``allow_auto_merge`` from the repository
    payload, so ``repository_info()`` reports nothing and the gate raises Held. A
    hosted Verify job that asked the gate could therefore never pass, whatever the
    candidate contained.
    """
    from touchstone.hosted import runtime
    from touchstone.runner import Held

    worktree = tmp_path / "resume-worktree"
    worktree.mkdir()
    projection = SimpleNamespace(
        finding_id="candidate-read-scoped",
        pr=31,
        head_sha="b" * 40,
        branch="touchstone/candidate-read-scoped",
        loop="code",
    )
    read_scoped_forge = SimpleNamespace(
        # Exactly what a contents:read token yields for both gate inputs.
        repository_info=lambda: None,
        labels=lambda: set(),
        pull=lambda _number: SimpleNamespace(
            head_sha="b" * 40,
            branch="touchstone/candidate-read-scoped",
            closed=False,
            merged_at=None,
        ),
    )
    context = SimpleNamespace(
        forge=read_scoped_forge,
        executor=SimpleNamespace(run=lambda *_args, **_kwargs: Result(0, "", "")),
    )
    config = _config(tmp_path)
    config.loop = lambda _name: SimpleNamespace(targets=("root",), label="touchstone")
    monkeypatch.setattr(runtime, "_checkout_resume_worktree", lambda *_args: worktree)
    monkeypatch.setattr(runtime, "_remove_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("touchstone.runner._health_gate", lambda *_args: None)
    # Deliberately NOT stubbing _publication_gate: reintroducing it must fail here.
    monkeypatch.setattr("touchstone.runner.current", lambda: context)
    monkeypatch.setattr(
        "touchstone.validation.validate",
        lambda *_args, **_kwargs: SimpleNamespace(blocked=False),
    )

    try:
        runtime._validate_resume_candidate(
            config,
            projection,
            context,
            {"GH_TOKEN": "read-token", "TOUCHSTONE_STATE_KEY": "state-secret"},
        )
    except Held as exc:  # pragma: no cover - the regression this test guards
        pytest.fail(f"verify consulted a push-scoped gate: {exc}")
