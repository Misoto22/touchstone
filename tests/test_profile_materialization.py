from __future__ import annotations

import json
from pathlib import Path

import pytest

from touchstone.cli import main
from touchstone.config import ConfigError, load
from touchstone.discovery import ProjectDiscovery
from touchstone.execution.local import LocalExecutor
from touchstone.initialize import InitOptions, initialize
from touchstone.profiles.materialize import profile_diff, refresh_profiles


def _next_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "next-repo"
    repository.mkdir()
    (repository / "package.json").write_text(
        json.dumps(
            {
                "name": "next-repo",
                "dependencies": {"next": "15.0.0", "react": "19.0.0"},
                "devDependencies": {"typescript": "5.8.0"},
            }
        ),
        encoding="utf-8",
    )
    (repository / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    return repository


def _options(repository: Path, **changes: object) -> InitOptions:
    values: dict[str, object] = {
        "start": repository,
        "engine": "codex",
        "model": "gpt-test",
        "workflows": ("ci.yml",),
        "schedule": "hourly@00",
        "discovered": ProjectDiscovery(
            repository,
            "acme/next-repo",
            "main",
            ("codex",),
            "launchd",
        ),
    }
    values.update(changes)
    return InitOptions(**values)  # type: ignore[arg-type]


def test_init_writes_project_and_generated_files(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)

    report = initialize(_options(repository), LocalExecutor())
    config = load(report.root)

    assert report.root == repository / "touchstone.toml"
    assert report.generated == repository / ".touchstone/generated.toml"
    assert config.targets["next-repo"].profiles == (
        "javascript",
        "typescript",
        "react",
        "nextjs",
    )
    assert config.loop("code").targets == ("next-repo",)
    assert config.source.schema_version == 2
    assert "/Users/" not in report.root.read_text(encoding="utf-8")


def test_refresh_preserves_root_overrides_and_is_noop_without_drift(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository), LocalExecutor())
    root_text = report.root.read_text(encoding="utf-8")
    report.root.write_text(
        root_text.replace("timeout_seconds = 2700", "timeout_seconds = 1234"),
        encoding="utf-8",
    )

    refresh = refresh_profiles(report.root, write=True)

    assert refresh.changed is False
    assert load(report.root).engine.timeout_seconds == 1234


def test_profile_diff_and_refresh_replace_only_generated_file(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository), LocalExecutor())
    root_before = report.root.read_bytes()
    report.generated.write_text(
        report.generated.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )

    diff = profile_diff(load(report.root))
    check = refresh_profiles(report.root, write=False)

    assert diff.changed is True
    assert "# edited" in diff.diff
    assert check.changed is True
    assert report.root.read_bytes() == root_before
    assert "# edited" in report.generated.read_text(encoding="utf-8")

    written = refresh_profiles(report.root, write=True)
    assert written.written is True
    assert report.root.read_bytes() == root_before
    assert "# edited" not in report.generated.read_text(encoding="utf-8")


def test_ambiguous_package_manager_requires_an_explicit_choice(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repository / "yarn.lock").write_text("# yarn\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="package manager"):
        initialize(_options(repository), LocalExecutor())

    report = initialize(_options(repository, package_manager="npm"), LocalExecutor())
    assert load(report.root).generated_metadata.package_manager == "npm"  # type: ignore[union-attr]


def test_profile_detect_json_is_read_only(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    repository = _next_repository(tmp_path)

    code = main(["profile", "detect", "--path", str(repository), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["targets"][0]["profiles"] == [
        "javascript",
        "typescript",
        "react",
        "nextjs",
    ]
    assert not (repository / "touchstone.toml").exists()


def test_profile_diff_cli_uses_exit_three_for_drift(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository), LocalExecutor())
    report.generated.write_text("# drift\n", encoding="utf-8")

    assert main(["--config", str(report.root), "profile", "diff"]) == 3


def test_explicit_profile_selection_survives_refresh(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository, profiles=("python",)), LocalExecutor())

    config = load(report.root)

    assert "python" in config.targets["next-repo"].profiles
    assert profile_diff(config).changed is False


def test_refresh_drops_stale_detected_profiles_without_project_override(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository), LocalExecutor())
    (repository / "package.json").write_text(
        json.dumps(
            {
                "name": "next-repo",
                "devDependencies": {"typescript": "5.8.0"},
            }
        ),
        encoding="utf-8",
    )

    diff = profile_diff(load(report.root))
    profiles = diff.materialized.data["target"]["next-repo"]["profiles"]

    assert profiles == ["javascript", "typescript"]
    assert "react" not in diff.expected_text
    assert "nextjs" not in diff.expected_text


def test_monorepo_records_package_manager_per_target(tmp_path: Path) -> None:
    repository = tmp_path / "mixed"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*","services/*"]}', encoding="utf-8"
    )
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    web = repository / "apps" / "web"
    api = repository / "services" / "api"
    web.mkdir(parents=True)
    api.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    (api / "pyproject.toml").write_text('[project]\nname="api"\n', encoding="utf-8")
    (api / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    report = initialize(_options(repository), LocalExecutor())
    config = load(report.root)

    assert config.targets["web"].package_managers == ("npm",)
    assert config.targets["api"].package_managers == ("uv",)
