from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.profiles import TargetCandidate, detect_profiles, load_catalog

FIXTURES = Path(__file__).parent / "fixtures/profiles"


def _confirmed(path: Path) -> set[str]:
    target = TargetCandidate(id=path.name, path=Path("."))
    return {
        match.profile for match in detect_profiles(path, target) if match.verdict == "confirmed"
    }


@pytest.mark.parametrize(
    ("fixture", "confirmed"),
    [
        ("next-typescript", {"javascript", "typescript", "react", "nextjs"}),
        ("django-uv", {"python", "django"}),
        ("fastapi-poetry", {"python", "fastapi"}),
        ("react-library", {"javascript", "react"}),
        ("node-cli", {"javascript", "node"}),
    ],
)
def test_profile_evidence_is_composable(fixture: str, confirmed: set[str]) -> None:
    path = FIXTURES / fixture
    matches = detect_profiles(path, TargetCandidate(id=fixture, path=Path(".")))

    assert {match.profile for match in matches if match.verdict == "confirmed"} == confirmed
    assert all(match.evidence for match in matches)


def test_package_json_alone_confirms_javascript_but_not_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"browser-code"}', encoding="utf-8")

    assert _confirmed(tmp_path) == {"javascript"}


def test_ruff_only_pyproject_does_not_confirm_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")

    assert _confirmed(tmp_path) == {"generic"}


def test_detector_never_imports_or_executes_repository_configuration(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"next":"15.0.0","react":"19.0.0"}}', encoding="utf-8"
    )
    (tmp_path / "next.config.js").write_text(
        "require('fs').writeFileSync('executed', 'bad')", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="safe"\ndependencies=["Django>=5"]\n', encoding="utf-8"
    )
    (tmp_path / "settings.py").write_text(
        "from pathlib import Path\nPath('executed').write_text('bad')", encoding="utf-8"
    )

    matches = detect_profiles(tmp_path, TargetCandidate(id="safe", path=Path(".")))

    assert {match.profile for match in matches if match.verdict == "confirmed"} == {
        "javascript",
        "react",
        "nextjs",
        "python",
        "django",
    }
    assert not (tmp_path / "executed").exists()


def test_builtin_catalog_loads_and_rejects_executable_local_profile(tmp_path: Path) -> None:
    catalog = load_catalog()
    assert tuple(catalog.profiles) == (
        "generic",
        "javascript",
        "node",
        "typescript",
        "react",
        "nextjs",
        "python",
        "fastapi",
        "django",
    )
    local = tmp_path / "profiles"
    local.mkdir()
    (local / "unsafe.toml").write_text(
        'name="unsafe"\nversion="1"\nmodule="project.detector"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="module"):
        load_catalog(local)


def test_repository_local_declarative_profile_participates_in_detection(
    tmp_path: Path,
) -> None:
    local = tmp_path / ".touchstone" / "profiles"
    local.mkdir(parents=True)
    (local / "vue.toml").write_text(
        'name = "vue"\n'
        'version = "1"\n'
        'category = "framework"\n'
        'supported = ">=3,<4"\n'
        'audit_context = "Inspect Vue components."\n'
        "protected_paths = []\n"
        'source_paths = ["src/"]\n'
        '[[detect]]\nkind = "dependency"\necosystem = "node"\nname = "vue"\n',
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text('{"dependencies":{"vue":"3.5.0"}}', encoding="utf-8")
    catalog = load_catalog(local)

    matches = detect_profiles(
        tmp_path,
        TargetCandidate(id="app", path=Path(".")),
        catalog=catalog,
    )

    assert any(match.profile == "vue" and match.verdict == "confirmed" for match in matches)


def test_unresolved_framework_version_requires_confirmation(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"next":"workspace:*"}}', encoding="utf-8"
    )

    matches = detect_profiles(tmp_path, TargetCandidate(id="app", path=Path(".")))
    nextjs = next(match for match in matches if match.profile == "nextjs")

    assert nextjs.verdict == "candidate"
    assert "version" in nextjs.warning.lower()
