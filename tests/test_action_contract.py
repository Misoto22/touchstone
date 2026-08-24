from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_accepts_only_explicit_stages() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts/action-entrypoint.sh").read_text(encoding="utf-8")

    assert "stage:" in action
    assert "$GITHUB_ACTION_PATH/scripts/action-entrypoint.sh" in action
    assert "set -euo pipefail" in entrypoint
    assert "prepare|analysis|publish|snapshot" in entrypoint
    assert "eval " not in entrypoint
    assert 'touchstone hosted "$stage"' in entrypoint


def test_action_installs_an_exact_package_version_in_isolation() -> None:
    entrypoint = (ROOT / "scripts/action-entrypoint.sh").read_text(encoding="utf-8")

    assert "python -m venv" in entrypoint
    assert "touchstone-agent==${version}" in entrypoint
