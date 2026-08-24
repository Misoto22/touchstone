from __future__ import annotations

import base64
import datetime as dt
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.hosted.crypto import BundleManifest, encrypt_bundle
from touchstone.hosted.runtime import CandidateMetadata, run_stage
from touchstone.hosted.snapshot import config_digest
from touchstone.ledger import Ledger
from touchstone.outcomes import ChangeState, RunOutcome, RunResult
from touchstone.runner import Held, _partial_write_gate

_PATCH = "diff --git a/a.txt b/a.txt\n"


def _key() -> bytes:
    return bytes(range(32))


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    loop = SimpleNamespace(name="code", schedule="hourly@00", priority=10, targets=())
    config = SimpleNamespace(
        repo_path=tmp_path,
        state_dir=tmp_path / ".touchstone" / "state",
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(slug="acme/widgets", default_branch="main"),
        execution=SimpleNamespace(target="local"),
        loops={"code": loop},
        timezone="UTC",
        generated_metadata=SimpleNamespace(source_digest="profile-digest"),
        engine=SimpleNamespace(name="codex"),
    )
    config.loop = lambda name: config.loops[name]
    return config


def _candidate() -> CandidateMetadata:
    return CandidateMetadata(
        repository="acme/widgets",
        loop="code",
        finding_id="finding1",
        candidate_id="candidate1",
        run_id="12345-1",
        base_sha="a" * 40,
        patch_digest=f"sha256:{hashlib.sha256(_PATCH.encode()).hexdigest()}",
        branch="touchstone/finding1-candidate1",
        finding={"title": "Unbounded retry loop", "commit_subject": "fix: bound the retry loop"},
        risk="high",
        verdict="skipped",
        verdict_reason="operator review required",
    )


def _write_candidate_artifact(
    config, tmp_path: Path, metadata: CandidateMetadata, *, run_id: str = "12345-1"
) -> Path:  # type: ignore[no-untyped-def]
    staging = tmp_path / "candidate-source"
    staging.mkdir(exist_ok=True)
    (staging / "candidate.json").write_text(metadata.to_json(), encoding="utf-8")
    (staging / "candidate.patch").write_text(_PATCH, encoding="utf-8")
    bundle = encrypt_bundle(
        BundleManifest(
            repository=config.forge.slug,
            loop=metadata.loop,
            schema_version=config.source.schema_version,
            config_digest=config_digest(config),
            profile_digest=config.generated_metadata.source_digest,
            lineage=metadata.candidate_id,
            run_id=run_id,
            created_at="2026-08-24T12:00:00Z",
        ),
        {
            "candidate.json": staging / "candidate.json",
            "candidate.patch": staging / "candidate.patch",
        },
        _key(),
    )
    destination = tmp_path / ".touchstone" / "hosted" / "inputs" / "candidate"
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "candidate.bundle.json"
    path.write_text(bundle.to_json(), encoding="utf-8")
    return path


def _analyzed(config, tmp_path: Path, monkeypatch, metadata: CandidateMetadata):  # type: ignore[no-untyped-def]
    env = {
        "TOUCHSTONE_STATE_KEY": base64.urlsafe_b64encode(_key()).decode(),
        "TOUCHSTONE_NOW": dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC).isoformat(),
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    monkeypatch.setattr("touchstone.hosted.runtime._ensure_engine", lambda *_args: None)
    monkeypatch.setattr(
        "touchstone.hosted.runtime._analyze_loop",
        lambda *_args, **_kwargs: (
            RunResult(
                RunOutcome.COMPLETED,
                lifecycle=ChangeState.PROPOSED,
                candidate_id=metadata.candidate_id,
            ),
            metadata,
        ),
    )
    assert run_stage(config, "analysis", env=env).outcome == "proposed"
    _write_candidate_artifact(config, tmp_path, metadata)
    return env


@pytest.mark.parametrize("job_result", ["failure", "cancelled"])
def test_an_abrupt_publish_leaves_a_durable_partial_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_result: str
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)

    snapshot = run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": job_result,
        },
    )

    projection = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(metadata.candidate_id)
    assert projection is not None
    assert projection.state == ChangeState.FAILED
    assert projection.partial is True
    assert projection.branch == metadata.branch
    assert projection.loop == "code"
    assert projection.title == "Unbounded retry loop"
    assert projection.risk == "high"
    assert projection.pr is None
    assert snapshot.partial is True
    assert snapshot.candidate_id == metadata.candidate_id


def test_the_partial_marker_survives_the_snapshot_and_blocks_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)
    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "failure",
        },
    )

    # The next hosted run starts from the encrypted snapshot alone.
    hosted = tmp_path / ".touchstone" / "hosted"
    shutil.rmtree(config.state_dir)
    (hosted / "prepare").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        hosted / "snapshot" / "state.bundle.json",
        hosted / "prepare" / "state.bundle.json",
    )
    shutil.rmtree(hosted / "inputs")
    shutil.rmtree(hosted / "candidate")

    with pytest.raises(Held, match="partial remote publication"):
        run_stage(config, "analysis", env=env)


def test_reconcile_can_reach_the_exact_partial_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)
    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "failure",
        },
    )
    projection = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(metadata.candidate_id)

    assert projection is not None
    assert projection.branch.startswith("touchstone/")
    assert ".." not in projection.branch


def test_a_successful_publish_records_no_extra_partial_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)

    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_OUTCOME": "completed",
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_FINAL_CHANGE_STATE": "awaiting_human",
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "success",
        },
    )

    assert Ledger(Path(config.state_dir) / "ledger.jsonl").projections() == {}
    _partial_write_gate(config)


def test_an_outcome_publish_already_recorded_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted.runtime import _write_state_bundle
    from touchstone.ledger import LifecycleEvent

    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)
    ledger = Ledger(Path(config.state_dir) / "ledger.jsonl")
    ledger.append(
        LifecycleEvent(
            finding_id=metadata.candidate_id,
            state=ChangeState.AWAITING_HUMAN,
            title="Unbounded retry loop",
            loop="code",
            risk="high",
            pr=12,
            head_sha="b" * 40,
            branch=metadata.branch,
        )
    )
    # Publish recorded its own outcome, then the job died during artifact upload.
    _write_state_bundle(
        config,
        tmp_path / ".touchstone" / "hosted" / "inputs" / "publish" / "publish-state.bundle.json",
        key=_key(),
        run_id="12345-1",
        result=RunResult(
            RunOutcome.COMPLETED,
            lifecycle=ChangeState.AWAITING_HUMAN,
            candidate_id=metadata.candidate_id,
        ),
    )

    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "failure",
        },
    )

    projection = ledger.projection(metadata.candidate_id)
    assert projection is not None
    assert projection.state == ChangeState.AWAITING_HUMAN
    assert projection.pr == 12


def test_an_unauthenticated_candidate_never_becomes_a_partial_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)
    artifact = tmp_path / ".touchstone" / "hosted" / "inputs" / "candidate"
    _write_candidate_artifact(config, tmp_path, metadata)
    forged = (artifact / "candidate.bundle.json").read_text(encoding="utf-8")
    (artifact / "candidate.bundle.json").write_text(
        forged.replace('"lineage":"candidate1"', '"lineage":"candidate2"'), encoding="utf-8"
    )

    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": "candidate2",
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "failure",
        },
    )

    assert Ledger(Path(config.state_dir) / "ledger.jsonl").projections() == {}


def test_the_due_slot_records_the_partial_result_not_the_pre_marker_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.scheduling.store import DueStore

    finished: list[RunResult] = []
    original = DueStore.finish

    def capture(self, claim, result, **kwargs):  # type: ignore[no-untyped-def]
        finished.append(result)
        return original(self, claim, result, **kwargs)

    monkeypatch.setattr(DueStore, "finish", capture)
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)

    run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "failure",
        },
    )

    assert finished
    assert finished[-1].partial is True
    assert finished[-1].candidate_id == metadata.candidate_id
    assert finished[-1].reason_code == "hosted-publish-unrecorded"


def test_a_clean_start_snapshot_still_records_an_abrupt_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    metadata = _candidate()
    env = _analyzed(config, tmp_path, monkeypatch, metadata)
    hosted = tmp_path / ".touchstone" / "hosted"
    # Every state artifact is lost; only the authenticated candidate survives.
    shutil.rmtree(hosted / "candidate")
    shutil.rmtree(config.state_dir)

    snapshot = run_stage(
        config,
        "snapshot",
        env=env
        | {
            "TOUCHSTONE_FINAL_CANDIDATE_ID": metadata.candidate_id,
            "TOUCHSTONE_FINAL_LOOP": metadata.loop,
            "TOUCHSTONE_PUBLISH_JOB_RESULT": "cancelled",
        },
    )

    projection = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(metadata.candidate_id)
    assert projection is not None
    assert projection.partial is True
    assert projection.state == ChangeState.FAILED
    assert snapshot.partial is True
    with pytest.raises(Held, match="partial remote publication"):
        _partial_write_gate(config)
