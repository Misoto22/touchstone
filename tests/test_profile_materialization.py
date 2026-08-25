from __future__ import annotations

import json
import tomllib
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


def test_refresh_preserves_configured_target_identity_by_path(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    report = initialize(_options(repository), LocalExecutor())
    report.root.write_text(
        report.root.read_text(encoding="utf-8").replace(
            'targets = ["next-repo"]', 'targets = ["legacy-root"]'
        ),
        encoding="utf-8",
    )
    report.generated.write_text(
        report.generated.read_text(encoding="utf-8").replace("next-repo", "legacy-root"),
        encoding="utf-8",
    )

    diff = profile_diff(load(report.root))

    assert "legacy-root" in diff.materialized.data["target"]
    assert "next-repo" not in diff.materialized.data["target"]


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


def test_explicit_profile_promotes_a_floating_version_candidate(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    package = json.loads((repository / "package.json").read_text(encoding="utf-8"))
    package["dependencies"]["next"] = "workspace:*"
    (repository / "package.json").write_text(json.dumps(package), encoding="utf-8")

    report = initialize(_options(repository, profiles=("nextjs",)), LocalExecutor())

    assert "nextjs" in load(report.root).targets["next-repo"].profiles


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


def test_target_local_ambiguous_lockfiles_accept_an_explicit_manager(tmp_path: Path) -> None:
    repository = tmp_path / "target-locks"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*"]}', encoding="utf-8"
    )
    web = repository / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    (web / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (web / "yarn.lock").write_text("# yarn\n", encoding="utf-8")

    report = initialize(
        _options(repository, package_manager="npm"),
        LocalExecutor(),
    )

    assert load(report.root).targets["web"].package_managers == ("npm",)


def test_explicit_profile_promotes_only_targets_with_matching_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "mixed-profiles"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*","services/*"]}', encoding="utf-8"
    )
    web = repository / "apps" / "web"
    api = repository / "services" / "api"
    web.mkdir(parents=True)
    api.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"dependencies":{"next":"workspace:*","react":"19.0.0"}}', encoding="utf-8"
    )
    (api / "pyproject.toml").write_text(
        '[project]\nname="api"\ndependencies=["django==5.2.0"]\n', encoding="utf-8"
    )

    report = initialize(_options(repository, profiles=("nextjs",)), LocalExecutor())
    config = load(report.root)

    assert "nextjs" in config.targets["web"].profiles
    assert "nextjs" not in config.targets["api"].profiles


@pytest.mark.parametrize(
    ("lockfile", "manager", "script_argv", "executable_argv"),
    [
        ("package-lock.json", "npm", ["npm", "run", "test"], ["npx", "tsc", "--noEmit"]),
        ("pnpm-lock.yaml", "pnpm", ["pnpm", "run", "test"], ["pnpm", "exec", "tsc", "--noEmit"]),
        ("yarn.lock", "yarn", ["yarn", "run", "test"], ["yarn", "run", "tsc", "--noEmit"]),
        ("bun.lock", "bun", ["bun", "run", "test"], ["bun", "x", "tsc", "--noEmit"]),
    ],
)
def test_generated_validation_uses_the_selected_javascript_package_manager(
    tmp_path: Path,
    lockfile: str,
    manager: str,
    script_argv: list[str],
    executable_argv: list[str],
) -> None:
    repository = _next_repository(tmp_path)
    (repository / lockfile).write_text("{}\n", encoding="utf-8")

    report = initialize(_options(repository), LocalExecutor())
    target = load(report.root).targets["next-repo"]
    argvs = [list(gate.argv) for gate in target.validation]

    assert target.package_managers == (manager,)
    assert script_argv in argvs
    assert executable_argv in argvs
    # Every Gate that runs project code stays a disabled Candidate. The base
    # Profile's source-read Gate is the only one any Profile turns on, and it
    # reaches a Target whose stack was detected too.
    assert [list(gate.argv) for gate in target.validation if gate.enabled] == [
        ["git", "diff", "--check"]
    ]


def test_generated_validation_keeps_npm_form_without_javascript_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "python-only"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname="api"\nversion="0"\ndependencies=["django>=5,<6"]\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    report = initialize(_options(repository), LocalExecutor())
    target = load(report.root).targets["api"]

    assert target.package_managers == ("uv",)
    assert [list(gate.argv) for gate in target.validation] == [
        ["git", "diff", "--check"],
        ["python", "-m", "pytest"],
        ["python", "manage.py", "check"],
    ]


def test_refresh_keeps_an_explicitly_configured_nested_standalone_target(tmp_path: Path) -> None:
    repository = tmp_path / "workspace"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*"]}', encoding="utf-8"
    )
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    web = repository / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    tools = repository / "tools" / "cli"
    tools.mkdir(parents=True)
    (tools / "package.json").write_text('{"name":"cli"}', encoding="utf-8")

    report = initialize(_options(repository), LocalExecutor())
    detected = load(report.root)
    assert "cli" not in detected.targets

    # The operator adopts the nested standalone project by naming it explicitly.
    report.generated.write_text(
        report.generated.read_text(encoding="utf-8")
        + '\n[target.cli]\npath = "tools/cli"\nprofiles = []\n',
        encoding="utf-8",
    )
    report.root.write_text(
        report.root.read_text(encoding="utf-8").replace(
            'targets = ["web"]', 'targets = ["web", "cli"]'
        ),
        encoding="utf-8",
    )

    diff = profile_diff(load(report.root))

    assert diff.materialized.data["target"]["cli"]["path"] == "tools/cli"
    assert diff.materialized.data["target"]["cli"]["package_managers"] == ["npm"]
    assert "tools/cli" not in [
        candidate.path.as_posix() for candidate in diff.materialized.discovery.candidates
    ]


def test_refresh_ignores_a_configured_path_without_a_manifest(tmp_path: Path) -> None:
    repository = tmp_path / "workspace-missing"
    repository.mkdir()
    (repository / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*"]}', encoding="utf-8"
    )
    web = repository / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")

    report = initialize(_options(repository), LocalExecutor())
    report.generated.write_text(
        report.generated.read_text(encoding="utf-8")
        + '\n[target.ghost]\npath = "tools/ghost"\nprofiles = []\n',
        encoding="utf-8",
    )
    (repository / "tools" / "ghost").mkdir(parents=True)

    diff = profile_diff(load(report.root))

    assert "ghost" not in diff.materialized.data["target"]


def test_generated_example_matches_a_real_generic_repository(tmp_path: Path) -> None:
    import tomllib

    repository = tmp_path / "generic"
    repository.mkdir()
    (repository / "NOTES.md").write_text("placeholder\n", encoding="utf-8")

    report = initialize(
        _options(
            repository,
            discovered=ProjectDiscovery(
                repository, "acme/repository", "main", ("codex",), "launchd"
            ),
        ),
        LocalExecutor(),
    )
    produced = tomllib.loads(report.generated.read_text(encoding="utf-8"))
    example = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "touchstone.generated.example.toml").read_text(
            encoding="utf-8"
        )
    )

    produced["metadata"]["source_digest"] = "sha256:example"
    assert produced == example


def test_the_base_gate_reaches_a_target_whose_stack_was_detected(tmp_path: Path) -> None:
    # `generic` is attached as a Match only when nothing else is detected, so
    # composing Gates from Matches alone left the one Gate any Profile enables
    # by default reaching exactly the repositories Touchstone could not
    # identify. `validate` was a no-op on every other repository.
    repository = _next_repository(tmp_path)

    report = initialize(_options(repository), LocalExecutor())
    target = load(report.root).targets["next-repo"]

    assert target.profiles == ("javascript", "typescript", "react", "nextjs")
    enabled = [gate for gate in target.validation if gate.enabled]
    assert [list(gate.argv) for gate in enabled] == [["git", "diff", "--check"]]
    assert enabled[0].capability == "source-read"


def _flat_layout_python_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "flat-api"
    (repository / "flat_api").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname="flat-api"\nversion="0"\ndependencies=["fastapi>=0.115,<1"]\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repository / "flat_api" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "flat_api" / "pricing.py").write_text("TOTAL = 0\n", encoding="utf-8")
    (repository / "tests" / "test_pricing.py").write_text("def test_it(): ...\n", encoding="utf-8")
    return repository


def test_source_paths_cover_a_flat_layout_python_package(tmp_path: Path) -> None:
    # The `python` Profile declares a `src/` layout. Materializing that guess
    # verbatim produced a `require_change_under` naming only directories this
    # Target does not have, so every source-only change it made was discarded.
    repository = _flat_layout_python_repository(tmp_path)

    report = initialize(_options(repository), LocalExecutor())
    config = load(report.root)
    generated = tomllib.loads(report.generated.read_text(encoding="utf-8"))

    assert generated["target"]["flat-api"]["source_paths"] == ["tests/", "flat_api/"]
    assert config.loops["code"].require_change_under == ("tests/", "flat_api/")


def test_source_paths_drop_layouts_the_target_does_not_have(tmp_path: Path) -> None:
    repository = _next_repository(tmp_path)
    (repository / "app").mkdir()

    report = initialize(_options(repository), LocalExecutor())
    config = load(report.root)

    # `src/`, `lib/` and `pages/` are declared by the Profiles and absent here.
    assert config.loops["code"].require_change_under == ("app/",)


def test_scoped_source_paths_keep_their_directory_separator(tmp_path: Path) -> None:
    repository = tmp_path / "workspace"
    (repository / "apps" / "web" / "app").mkdir(parents=True)
    (repository / "package.json").write_text(
        json.dumps({"name": "workspace", "private": True, "workspaces": ["apps/*"]}),
        encoding="utf-8",
    )
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repository / "apps" / "web" / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "15.0.0"}}),
        encoding="utf-8",
    )

    report = initialize(
        _options(
            repository,
            discovered=ProjectDiscovery(
                repository, "acme/workspace", "main", ("codex",), "launchd"
            ),
        ),
        LocalExecutor(),
    )
    config = load(report.root)

    # Without the separator `apps/web/apple.ts` reads as a change under this.
    assert "apps/web/app/" in config.loops["code"].require_change_under


def test_a_target_without_a_recognised_layout_keeps_its_own_directory(tmp_path: Path) -> None:
    repository = tmp_path / "workspace"
    (repository / "packages" / "tool").mkdir(parents=True)
    (repository / "package.json").write_text(
        json.dumps({"name": "workspace", "private": True, "workspaces": ["packages/*"]}),
        encoding="utf-8",
    )
    (repository / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repository / "packages" / "tool" / "package.json").write_text(
        json.dumps({"name": "tool", "dependencies": {"react": "19.0.0"}}),
        encoding="utf-8",
    )
    (repository / "packages" / "tool" / "index.js").write_text("module.exports = {}\n", "utf-8")

    report = initialize(
        _options(
            repository,
            discovered=ProjectDiscovery(
                repository, "acme/workspace", "main", ("codex",), "launchd"
            ),
        ),
        LocalExecutor(),
    )
    config = load(report.root)

    # A leaf Target that keeps its code at its own root is still maintained by
    # the loop. The repository root is not added: it is under every path, and
    # claiming it would neuter the gate for every other Target.
    assert "packages/tool/" in config.loops["code"].require_change_under
    assert "./" not in config.loops["code"].require_change_under
