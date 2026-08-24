from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.cli import main
from touchstone.config import ConfigError
from touchstone.hosted.workflow import ActionPins, actions_diff, render_workflow


def _config(tmp_path: Path, *, visibility: str = "public", wake_minutes: int | None = None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        repo_path=tmp_path,
        forge=SimpleNamespace(default_branch="main"),
        engine=SimpleNamespace(name="codex"),
        actions=SimpleNamespace(
            visibility=visibility,
            wake_minutes=(15 if visibility == "public" else 60)
            if wake_minutes is None
            else wake_minutes,
            artifact_retention_days=90,
            node_version="24",
            approval_environment="",
            auto_merge=False,
        ),
    )


def _refs(text: str) -> list[str]:
    return re.findall(r"^\s*uses:\s*[^@\s]+@([0-9a-f]+)", text, re.MULTILINE)


def test_generated_workflow_exposes_split_trust_boundaries(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    prepare, analysis = text.split("  analysis:", 1)
    analysis, publish = analysis.split("  publish:", 1)
    publish, snapshot = publish.split("  snapshot:", 1)

    assert "pull_request:" not in text
    assert "schedule:" in text and "workflow_dispatch:" in text
    assert "cancel-in-progress: false" in text
    assert "secrets." not in prepare
    assert "TOUCHSTONE_APP_PRIVATE_KEY" not in analysis
    assert "OPENAI_API_KEY" not in publish
    assert "OPENAI_API_KEY" not in snapshot
    assert "auto-merge" not in text.lower()
    assert "retention-days: 90" in text


def test_credentials_are_action_inputs_not_install_step_environment(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    analysis = text.split("  analysis:", 1)[1].split("  publish:", 1)[0]

    assert "openai-api-key: ${{ secrets.OPENAI_API_KEY }}" in analysis
    assert "state-key: ${{ secrets.TOUCHSTONE_STATE_KEY }}" in analysis
    assert "env:\n          OPENAI_API_KEY:" not in analysis


def test_publish_verifies_without_write_token_before_minting_scoped_token(
    tmp_path: Path,
) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    publish = text.split("  publish:", 1)[1].split("  snapshot:", 1)[0]

    verify_at = publish.index("stage: verify")
    token_at = publish.index("id: app-token")
    publish_at = publish.index("stage: publish")
    assert verify_at < token_at < publish_at
    assert "persist-credentials: false" in publish
    assert "\n          token: ${{ steps.app-token.outputs.token }}" not in publish
    assert "repositories: ${{ github.event.repository.name }}" in publish
    assert publish.count("expected-loop: ${{ needs.analysis.outputs.loop }}") == 2
    assert publish.count("candidate-id: ${{ needs.analysis.outputs.candidate_id }}") == 2


def test_analysis_exports_independently_expected_candidate_identity(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    analysis = text.split("  analysis:", 1)[1].split("  publish:", 1)[0]

    assert "loop: ${{ steps.touchstone.outputs.loop }}" in analysis
    assert "candidate_id: ${{ steps.touchstone.outputs.candidate_id }}" in analysis


def test_snapshot_receives_final_stage_contract_for_due_slot_finalization(
    tmp_path: Path,
) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    snapshot = text.split("  snapshot:", 1)[1]

    assert "publish-job-result: ${{ needs.publish.result }}" in snapshot
    assert (
        "final-outcome: ${{ needs.publish.outputs.outcome || needs.analysis.outputs.outcome }}"
        in snapshot
    )
    assert "final-candidate-id:" in snapshot
    assert "final-change-state:" in snapshot
    assert "final-partial:" in snapshot


def test_every_action_reference_is_an_immutable_sha(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="b" * 40)

    refs = _refs(text)
    assert refs
    assert all(len(ref) == 40 for ref in refs)
    assert not re.search(r"uses:.*@(main|master|v\d+)\s*$", text, re.MULTILINE)


def test_public_and_private_wake_cadence_are_off_hour(tmp_path: Path) -> None:
    public = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    private = render_workflow(
        _config(tmp_path, visibility="private"), ActionPins(), action_sha="a" * 40
    )

    assert "cron: '7,22,37,52 * * * *'" in public
    assert "cron: '17 * * * *'" in private


def test_supported_custom_wake_cadence_is_rendered_without_visibility_coupling(
    tmp_path: Path,
) -> None:
    public = render_workflow(
        _config(tmp_path, visibility="public", wake_minutes=30),
        ActionPins(),
        action_sha="a" * 40,
    )
    private = render_workflow(
        _config(tmp_path, visibility="private", wake_minutes=15),
        ActionPins(),
        action_sha="a" * 40,
    )

    assert "cron: '7,37 * * * *'" in public
    assert "cron: '7,22,37,52 * * * *'" in private


def test_actions_diff_is_read_only_and_reports_drift(tmp_path: Path) -> None:
    rendered = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)

    first = actions_diff(tmp_path, rendered)
    assert first.changed and not first.path.exists()
    first.write()
    assert actions_diff(tmp_path, rendered).changed is False


def test_actions_init_writes_then_checks_exact_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("touchstone.cli.load", lambda _path: config)
    sha = "c" * 40

    assert main(["actions", "init", "--action-sha", sha]) == 0
    workflow = tmp_path / ".github" / "workflows" / "touchstone.yml"
    assert workflow.is_file()
    assert main(["actions", "init", "--action-sha", sha, "--check"]) == 0


def test_workflow_rejects_mutable_action_reference(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="40-character commit SHA"):
        render_workflow(_config(tmp_path), ActionPins(), action_sha="main")


def test_claude_workflow_uses_only_the_claude_model_secret(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.engine.name = "claude"

    workflow = render_workflow(config, ActionPins(), action_sha="a" * 40)
    analysis = workflow.split("  analysis:", 1)[1].split("  publish:", 1)[0]

    assert "ANTHROPIC_API_KEY" in analysis
    assert "OPENAI_API_KEY" not in analysis


def test_publish_environment_is_emitted_only_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.actions.approval_environment = "touchstone-publish"

    workflow = render_workflow(config, ActionPins(), action_sha="a" * 40)
    publish = workflow.split("  publish:", 1)[1].split("  snapshot:", 1)[0]

    assert 'name: "touchstone-publish"' in publish
