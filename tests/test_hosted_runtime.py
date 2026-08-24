from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.hosted.crypto import BundleManifest, encrypt_bundle
from touchstone.hosted.runtime import (
    CandidateIntegrityError,
    CandidateMetadata,
    HostedOutputs,
    run_stage,
    validate_stage_environment,
    verify_candidate,
)


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
    validate_stage_environment("publish", {"GH_TOKEN": "app-token"})

    with pytest.raises(CandidateIntegrityError, match="publishing credential"):
        validate_stage_environment(
            "analysis",
            {"OPENAI_API_KEY": "model", "TOUCHSTONE_APP_PRIVATE_KEY": "pem"},
        )
    with pytest.raises(CandidateIntegrityError, match="model credential"):
        validate_stage_environment(
            "publish", {"GH_TOKEN": "app-token", "OPENAI_API_KEY": "model"}
        )
    with pytest.raises(CandidateIntegrityError, match="credential"):
        validate_stage_environment("snapshot", {"GH_TOKEN": "app-token"})


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
        candidate_id="candidate-1",
        should_run=True,
    )

    output.write(tmp_path / "result.json")

    payload = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["candidate_id"] == "candidate-1"
    assert "candidate_id=candidate-1" in github_output.read_text(encoding="utf-8")
    assert "Analysis" in summary.read_text(encoding="utf-8")


def test_publish_verifies_candidate_before_any_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    patch = tmp_path / "candidate.patch"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    metadata = CandidateMetadata(
        repository="acme/widgets",
        loop="code",
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
    assert (
        tmp_path / ".touchstone" / "hosted" / "snapshot" / "state.bundle.json"
    ).is_file()
