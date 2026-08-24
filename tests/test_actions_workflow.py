from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.cli import main
from touchstone.config import ConfigError
from touchstone.hosted.workflow import (
    ActionPins,
    actions_diff,
    render_workflow,
    resolve_action_sha,
)


def _config(tmp_path: Path, *, visibility: str = "public", wake_minutes: int | None = None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        repo_path=tmp_path,
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(default_branch="main", slug="acme/widgets"),
        engine=SimpleNamespace(name="codex"),
        execution=SimpleNamespace(target="local", ssh=None),
        git=SimpleNamespace(),
        timezone="UTC",
        loops={},
        targets={},
        generated_metadata=None,
        actions=SimpleNamespace(
            visibility=visibility,
            wake_minutes=(15 if visibility == "public" else 60)
            if wake_minutes is None
            else wake_minutes,
            artifact_retention_days=90,
            node_version="24",
            action_sha="",
            approval_environment="",
            auto_merge=False,
        ),
    )


def _refs(text: str) -> list[str]:
    return re.findall(r"^\s*uses:\s*[^@\s]+@([0-9a-f]+)", text, re.MULTILINE)


def test_generated_workflow_exposes_split_trust_boundaries(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    prepare, analysis = text.split("  analysis:", 1)
    analysis, verify = analysis.split("  verify:", 1)
    verify, publish = verify.split("  publish:", 1)
    publish, snapshot = publish.split("  snapshot:", 1)

    assert "pull_request:" not in text
    assert "schedule:" in text and "workflow_dispatch:" in text
    assert "cancel-in-progress: false" in text
    assert "secrets." not in prepare
    assert "TOUCHSTONE_APP_PRIVATE_KEY" not in analysis
    assert "TOUCHSTONE_APP_PRIVATE_KEY" not in verify
    assert "OPENAI_API_KEY" not in verify
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


def test_verify_and_publish_use_different_jobs_and_publish_rebuilds_after_token_mint(
    tmp_path: Path,
) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    verify = text.split("  verify:", 1)[1].split("  publish:", 1)[0]
    publish = text.split("  publish:", 1)[1].split("  snapshot:", 1)[0]

    token_at = publish.index("id: app-token")
    publish_at = publish.index("stage: publish")
    assert "stage: verify" in verify
    assert "id: app-token" not in verify
    assert "stage: verify" not in publish
    assert token_at < publish_at
    assert "needs:\n      - analysis\n      - verify" in "  publish:" + publish
    assert "name: touchstone-verified-${{ github.run_id }}" in verify
    assert "name: touchstone-verified-${{ github.run_id }}" in publish
    assert "persist-credentials: false" in publish
    assert "repositories: ${{ github.event.repository.name }}" in publish
    assert publish.count("expected-loop: ${{ needs.analysis.outputs.loop }}") == 1
    assert publish.count("candidate-id: ${{ needs.analysis.outputs.candidate_id }}") == 1


def test_analysis_exports_independently_expected_candidate_identity(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    analysis = text.split("  analysis:", 1)[1].split("  publish:", 1)[0]

    assert "loop: ${{ steps.touchstone.outputs.loop }}" in analysis
    assert "candidate_id: ${{ steps.touchstone.outputs.candidate_id }}" in analysis
    assert (
        "name: touchstone-candidate-${{ steps.touchstone.outputs.candidate_id || github.run_id }}"
        in analysis
    )


def test_candidate_artifact_identity_follows_analysis_through_snapshot(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    verify = text.split("  verify:", 1)[1].split("  publish:", 1)[0]
    publish = text.split("  publish:", 1)[1].split("  snapshot:", 1)[0]
    snapshot = text.split("  snapshot:", 1)[1]
    downstream_name = (
        "name: touchstone-candidate-${{ needs.analysis.outputs.candidate_id || github.run_id }}"
    )

    assert downstream_name in verify
    assert downstream_name in publish
    assert downstream_name in snapshot
    assert "pattern: touchstone-*-${{ github.run_id }}" in snapshot


def test_state_artifacts_are_named_by_effective_config_not_run_order(tmp_path: Path) -> None:
    text = render_workflow(_config(tmp_path), ActionPins(), action_sha="a" * 40)
    snapshot = text.split("  snapshot:", 1)[1]

    assert re.search(r"name: touchstone-state-[0-9a-f]{64}", snapshot)
    assert "name: touchstone-state-${{ github.run_id }}" not in snapshot


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


def test_default_branch_is_escaped_as_a_github_expression_literal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.forge.default_branch = "release'candidate"

    workflow = render_workflow(config, ActionPins(), action_sha="a" * 40)

    assert "if: github.ref == 'refs/heads/release''candidate'" in workflow
    assert "refs/heads/release'candidate'" not in workflow


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


def test_default_action_reference_resolves_the_installed_release_tag(tmp_path: Path) -> None:
    import io
    import json as json_module
    from importlib.metadata import version

    from touchstone.hosted import workflow as workflow_module

    requested: list[str] = []

    class _Response(io.BytesIO):
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

    def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        requested.append(request.full_url)
        return _Response(json_module.dumps({"sha": "c" * 40}).encode())

    original = workflow_module.urllib.request.urlopen
    workflow_module.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        resolved = resolve_action_sha(_config(tmp_path))
    finally:
        workflow_module.urllib.request.urlopen = original  # type: ignore[assignment]

    assert resolved == "c" * 40
    assert requested == [
        f"https://api.github.com/repos/Misoto22/touchstone/commits/v{version('touchstone-agent')}"
    ]
    assert not any(url.endswith("/commits/main") for url in requested)


def test_explicit_action_sha_is_used_without_any_network_call(tmp_path: Path) -> None:
    from touchstone.hosted import workflow as workflow_module

    config = _config(tmp_path)
    config.actions.action_sha = "d" * 40

    original = workflow_module.urllib.request.urlopen

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("an explicit --action-sha must not query GitHub")

    workflow_module.urllib.request.urlopen = fail  # type: ignore[assignment]
    try:
        assert resolve_action_sha(config) == "d" * 40
    finally:
        workflow_module.urllib.request.urlopen = original  # type: ignore[assignment]


def test_a_development_version_refuses_to_guess_an_action_reference(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from touchstone.hosted import workflow as workflow_module

    monkeypatch.setattr(workflow_module, "version", lambda _name: "0.1.3.dev1+g0123abc")

    with pytest.raises(ConfigError, match="not a published release"):
        workflow_module.installed_release_tag()
