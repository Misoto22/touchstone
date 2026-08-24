from __future__ import annotations

from pathlib import Path

from touchstone.profiles.targets import affected_targets, discover_targets

FIXTURES = Path(__file__).parent / "fixtures/workspaces"


def test_explicit_workspace_members_become_stable_targets() -> None:
    found = discover_targets(FIXTURES / "mixed")

    assert [(target.id, target.path) for target in found.targets] == [
        ("web", Path("apps/web")),
        ("ui", Path("packages/ui")),
        ("api", Path("services/api")),
    ]
    assert [candidate.path for candidate in found.candidates] == [Path("tools/demo")]
    assert Path("examples") in found.excluded
    assert Path("services/legacy") in found.excluded
    assert found.workspace_container is True


def test_changed_shared_package_expands_reverse_dependencies() -> None:
    found = discover_targets(FIXTURES / "mixed")

    assert affected_targets(["packages/ui/button.tsx"], found) == ("ui", "web")
    assert affected_targets(["apps/web/app/page.tsx"], found) == ("web",)


def test_pnpm_members_and_exclusions_are_data_only(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'apps/*'\n  - '!apps/old'\n", encoding="utf-8"
    )
    for name in ("active", "old"):
        member = tmp_path / "apps" / name
        member.mkdir(parents=True)
        (member / "package.json").write_text(f'{{"name":"{name}"}}', encoding="utf-8")

    found = discover_targets(tmp_path)

    assert [(target.id, target.path) for target in found.targets] == [
        ("active", Path("apps/active"))
    ]
    assert Path("apps/old") in found.excluded


def test_uv_and_pdm_members_require_a_project_manifest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv.workspace]\n"
        'members = ["python/*"]\n'
        "[tool.pdm.workspace]\n"
        'members = ["services/*"]\n',
        encoding="utf-8",
    )
    valid = tmp_path / "python" / "worker"
    missing = tmp_path / "python" / "notes"
    pdm = tmp_path / "services" / "gateway"
    for path in (valid, missing, pdm):
        path.mkdir(parents=True)
    (valid / "pyproject.toml").write_text('[project]\nname="worker"\n', encoding="utf-8")
    (pdm / "pyproject.toml").write_text('[project]\nname="gateway"\n', encoding="utf-8")

    found = discover_targets(tmp_path)

    assert [(target.id, target.path) for target in found.targets] == [
        ("worker", Path("python/worker")),
        ("gateway", Path("services/gateway")),
    ]
    assert Path("python/notes") not in {target.path for target in found.targets}


def test_single_project_root_is_a_target_and_owns_nested_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="root-app"\n', encoding="utf-8")
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    found = discover_targets(tmp_path)

    target_id = tmp_path.name.replace("_", "-")
    assert [(target.id, target.path) for target in found.targets] == [(target_id, Path("."))]
    assert affected_targets(["src/feature/module.py"], found) == (target_id,)


def test_duplicate_directory_names_get_deterministic_suffixes(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"private":true,"workspaces":["apps/*","services/*"]}', encoding="utf-8"
    )
    for relative in ("apps/api", "services/api"):
        member = tmp_path / relative
        member.mkdir(parents=True)
        (member / "package.json").write_text(f'{{"name":"{relative}"}}', encoding="utf-8")

    found = discover_targets(tmp_path)

    assert [(target.id, target.path) for target in found.targets] == [
        ("api", Path("apps/api")),
        ("api-2", Path("services/api")),
    ]
    assert found.warnings == ("Target ID 'api' was disambiguated as 'api-2' for services/api",)


def test_submodules_dependencies_venvs_fixtures_and_vendor_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"root"}', encoding="utf-8")
    (tmp_path / ".gitmodules").write_text(
        '[submodule "external"]\npath = external/code\nurl = https://example.test/code\n',
        encoding="utf-8",
    )
    for relative in (
        "external/code",
        "node_modules/pkg",
        ".venv/pkg",
        "fixtures/sample",
        "vendor/pkg",
    ):
        path = tmp_path / relative
        path.mkdir(parents=True)
        (path / "package.json").write_text('{"name":"ignored"}', encoding="utf-8")

    found = discover_targets(tmp_path)

    assert found.candidates == ()
    assert set(found.excluded) >= {
        Path("external/code"),
        Path("node_modules"),
        Path(".venv"),
        Path("fixtures"),
        Path("vendor"),
    }


def test_deepest_target_owns_a_changed_file(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"root","workspaces":["packages/*"],"scripts":{"test":"true"}}',
        encoding="utf-8",
    )
    member = tmp_path / "packages" / "ui"
    member.mkdir(parents=True)
    (member / "package.json").write_text('{"name":"ui"}', encoding="utf-8")

    found = discover_targets(tmp_path)

    assert affected_targets(["packages/ui/index.js"], found) == ("ui",)
