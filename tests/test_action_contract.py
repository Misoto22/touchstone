from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_accepts_only_explicit_stages() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts/action-entrypoint.sh").read_text(encoding="utf-8")

    assert "stage:" in action
    assert "$GITHUB_ACTION_PATH/scripts/action-entrypoint.sh" in action
    assert "set -euo pipefail" in entrypoint
    assert "install|prepare|analysis|verify|publish|snapshot" in entrypoint
    assert "eval " not in entrypoint
    assert 'touchstone" hosted "$stage"' in entrypoint


def test_action_installs_the_pinned_action_checkout_before_mapping_credentials() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts/action-entrypoint.sh").read_text(encoding="utf-8")

    install, runtime = action.split("- name: Run Touchstone stage", 1)
    assert "inputs.openai-api-key" not in install
    assert "inputs.anthropic-api-key" not in install
    assert "inputs.state-key" not in install
    assert "inputs.github-token" not in install
    assert "OPENAI_API_KEY:" in runtime
    assert "ANTHROPIC_API_KEY:" in runtime
    assert "TOUCHSTONE_STATE_KEY:" in runtime
    assert "GH_TOKEN:" in runtime
    assert "TOUCHSTONE_FINAL_OUTCOME:" in runtime
    assert "TOUCHSTONE_PUBLISH_JOB_RESULT:" in runtime
    assert "python -m venv" in entrypoint
    assert "--require-hashes" in entrypoint
    assert '"$GITHUB_ACTION_PATH/action-requirements.lock"' in entrypoint
    assert "--no-build-isolation" in entrypoint
    assert "--no-deps" in entrypoint
    assert 'touchstone" hosted install --for-stage "$requested_stage"' in entrypoint
    assert 'action-entrypoint.sh install "${{ inputs.stage }}"' in install
    assert "touchstone-agent==" not in entrypoint


def test_project_preparation_runs_only_in_the_credential_free_install_step() -> None:
    entrypoint = (ROOT / "scripts/action-entrypoint.sh").read_text(encoding="utf-8")

    guard, install = entrypoint.split('if [[ "$stage" == "install" ]]; then', 1)
    del guard
    credential_guard, remainder = install.split("if [[", 1)
    del remainder
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TOUCHSTONE_STATE_KEY", "GH_TOKEN"):
        assert name in credential_guard
    assert 'hosted install --for-stage "$requested_stage"' in install
    assert "hosted install --for-stage" not in entrypoint.split("exec ", 1)[-1]


def test_agent_runtime_versions_are_read_from_the_lock_not_configuration() -> None:
    import dataclasses

    from touchstone.config import _ACTIONS, ActionsConfig

    retired = {"codex_cli_version", "claude_code_version"}
    example = (ROOT / "touchstone.example.toml").read_text(encoding="utf-8")

    assert retired.isdisjoint({field.name for field in dataclasses.fields(ActionsConfig)})
    assert retired.isdisjoint(_ACTIONS)
    assert retired.isdisjoint(example.split())
    for key in retired:
        assert key not in example


def test_hosted_agent_runtimes_are_integrity_locked() -> None:
    packages = {
        "codex": "@openai/codex",
        "claude": "@anthropic-ai/claude-code",
    }

    for engine, package in packages.items():
        directory = ROOT / "agent-runtime" / engine
        manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))
        version = manifest["dependencies"][package]

        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        assert manifest["dependencies"] == {package: version}
        assert lock["lockfileVersion"] == 3
        assert lock["packages"][""]["dependencies"] == {package: version}
        assert lock["packages"][f"node_modules/{package}"]["version"] == version
        assert all(
            "integrity" in package_metadata
            for package_path, package_metadata in lock["packages"].items()
            if package_path
        )

    requirements = (ROOT / "action-requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in requirements
    assert "hatchling==1.28.0" in requirements
