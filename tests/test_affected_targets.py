from __future__ import annotations

import subprocess
from pathlib import Path

from touchstone.config import load
from touchstone.discovery import ProjectDiscovery
from touchstone.execution.local import LocalExecutor
from touchstone.initialize import InitOptions, initialize
from touchstone.validation import affected_validation_targets, validate_affected


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
    )


def _monorepo(tmp_path: Path) -> Path:
    repository = tmp_path / "mixed"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"name":"root","private":true,"workspaces":["apps/*","packages/*","services/*"]}',
        encoding="utf-8",
    )
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repository / "README.md").write_text("shared\n", encoding="utf-8")
    web = repository / "apps" / "web"
    ui = repository / "packages" / "ui"
    api = repository / "services" / "api"
    for directory in (web, ui, api):
        directory.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"name":"web","dependencies":{"ui":"workspace:*"}}', encoding="utf-8"
    )
    (ui / "package.json").write_text('{"name":"ui"}', encoding="utf-8")
    (api / "package.json").write_text('{"name":"api"}', encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.test")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "seed")
    return repository


def _config(repository: Path):  # type: ignore[no-untyped-def]
    report = initialize(
        InitOptions(
            start=repository,
            engine="codex",
            model="model-test",
            workflows=("ci.yml",),
            schedule="hourly@00",
            discovered=ProjectDiscovery(repository, "acme/mixed", "main", ("codex",), "launchd"),
        ),
        LocalExecutor(),
    )
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "configure")
    return load(report.root)


def _touch(repository: Path, relative: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")


def test_a_direct_target_change_selects_only_that_target(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)
    _touch(repository, "packages/ui/button.tsx")

    selected = affected_validation_targets(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert "ui" in selected
    assert "api" not in selected


def test_a_dependency_change_also_selects_its_reverse_dependents(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)
    _touch(repository, "packages/ui/button.tsx")

    selected = affected_validation_targets(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert set(selected) == {"ui", "web"}
    assert "api" not in selected


def test_an_unrelated_target_is_skipped(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)
    assert set(config.loop("code").targets) == {"api", "ui", "web"}
    _touch(repository, "apps/web/page.tsx")

    selected = affected_validation_targets(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert selected == ("web",)


def test_a_shared_root_change_conservatively_selects_every_loop_target(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)
    _touch(repository, "README.md")

    selected = affected_validation_targets(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert selected == config.loop("code").targets
    assert len(selected) > 1


def test_an_unreadable_worktree_status_conservatively_selects_every_loop_target(
    tmp_path: Path,
) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)

    selected = affected_validation_targets(
        config,
        config.loop("code").targets,
        LocalExecutor(),
        repository=tmp_path / "not-a-repository",
    )

    assert selected == config.loop("code").targets


def test_validation_runs_gates_only_for_the_selected_targets(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    _config(repository)
    generated = repository / ".touchstone" / "generated.toml"
    generated.write_text(
        generated.read_text(encoding="utf-8").replace("enabled = false", "enabled = true"),
        encoding="utf-8",
    )
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "enable gates")
    config = load(repository / "touchstone.toml")
    _touch(repository, "apps/web/page.tsx")

    report = validate_affected(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert {result.target for result in report.results} == {"web"}
    assert all(result.reason != "disabled" for result in report.results)


def test_a_clean_worktree_runs_no_validation_gate(tmp_path: Path) -> None:
    repository = _monorepo(tmp_path)
    config = _config(repository)

    report = validate_affected(
        config, config.loop("code").targets, LocalExecutor(), repository=repository
    )

    assert report.results == ()
    assert report.outcome == "completed"
