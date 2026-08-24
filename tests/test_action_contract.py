from __future__ import annotations

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
    assert 'pip install --disable-pip-version-check "$GITHUB_ACTION_PATH"' in entrypoint
    assert "touchstone-agent==" not in entrypoint
