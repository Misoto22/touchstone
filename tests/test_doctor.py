from __future__ import annotations

import json
from pathlib import Path

from tests.test_config import _valid_config, _write
from touchstone.cli import main
from touchstone.config import load_config
from touchstone.doctor import DoctorContext, run_doctor


class MemoryForge:
    def __init__(self, *, labels: set[str] | None = None) -> None:
        self._labels = labels or set()

    def repository_info(self) -> dict[str, object]:
        return {
            "nameWithOwner": "acme/widgets",
            "defaultBranchRef": {"name": "trunk"},
            "autoMergeAllowed": True,
        }

    def labels(self) -> set[str]:
        return set(self._labels)

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
        return "success"


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _valid_config().replace('path = "."', 'path = "repo"')
    text = text.replace(
        'slug = "acme/widgets"',
        'slug = "acme/widgets"\nrequired_workflows = ["ci.yml"]',
    )
    return load_config(_write(tmp_path / "touchstone.toml", text))


def test_doctor_fails_before_sessions_when_engine_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    report = run_doctor(config, context)
    check = report.by_id("engine.command")

    assert (check.level, check.repair) == (
        "FAIL",
        "Install the configured 'codex' command and authenticate it.",
    )
    assert report.exit_code == 1


def test_doctor_json_contains_stable_checks_and_no_environment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    payload = json.loads(run_doctor(config, context).to_json())

    assert payload["exit_code"] == 0
    assert payload["checks"][0]["id"] == "config.schema"
    assert "environment" not in json.dumps(payload).lower()


def test_doctor_command_is_registered(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path)

    code = main(["--config", str(config.source.path), "doctor", "--json", "--offline"])

    output = json.loads(capsys.readouterr().out)
    assert code in {0, 1}
    assert output["checks"][0]["id"] == "config.schema"
