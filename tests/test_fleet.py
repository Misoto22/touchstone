"""One project's configuration fans out to many repositories."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from touchstone.fleet import FleetError, load_checkout_map, load_project, render, sync_check

PROJECT = """
version = 1
name = "personal"

[defaults]
timezone = "Australia/Sydney"

[defaults.engine]
name = "claude"
model = "strong-model"

[defaults.engine.cheap]
name = "claude"
model = "small-model"

[defaults.loop.hardcode]
brief = "builtin:hardcode"
label = "touchstone:hardcode"
schedule = "daily@03:00"
engine = "cheap"

[defaults.loop.naming]
brief = "builtin:naming"
label = "touchstone:naming"
schedule = "weekly@SUN,04:00"

[member."acme/widgets"]
path = "../widgets"
loops = ["hardcode", "naming"]

[member."acme/gadgets"]
path = "../gadgets"
loops = ["hardcode"]

[member."acme/gadgets".overrides.engine]
model = "gadget-model"
"""


def _project(tmp_path: Path, body: str = PROJECT) -> Path:
    path = tmp_path / "projects" / "personal.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_a_member_receives_only_the_loops_it_takes(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))

    widgets = tomllib.loads(render(project, "acme/widgets"))
    gadgets = tomllib.loads(render(project, "acme/gadgets"))

    assert set(widgets["loop"]) == {"hardcode", "naming"}
    assert set(gadgets["loop"]) == {"hardcode"}


def test_a_member_can_resolve_a_portable_logical_checkout(tmp_path: Path) -> None:
    body = PROJECT.replace('path = "../widgets"', 'checkout = "widgets"')
    checkout = tmp_path / "local/widgets"
    checkout.mkdir(parents=True)
    checkout_map = tmp_path / "harness.local.toml"
    checkout_map.write_text(f'[checkouts]\nwidgets = "{checkout}"\n', encoding="utf-8")

    project = load_project(_project(tmp_path, body), checkout_map=checkout_map)

    assert project.members["acme/widgets"].path == checkout.resolve()


def test_an_unresolved_logical_checkout_is_not_installed(tmp_path: Path) -> None:
    body = PROJECT.replace('path = "../widgets"', 'checkout = "widgets"')
    checkout_map = tmp_path / "harness.local.toml"
    checkout_map.write_text("[checkouts]\n", encoding="utf-8")

    with pytest.raises(FleetError) as error:
        load_project(_project(tmp_path, body), checkout_map=checkout_map)

    assert error.value.reason_code == "checkout-not-installed"


def test_checkout_map_accepts_only_absolute_paths(tmp_path: Path) -> None:
    checkout_map = tmp_path / "harness.local.toml"
    checkout_map.write_text('[checkouts]\nwidgets = "../widgets"\n', encoding="utf-8")

    with pytest.raises(FleetError, match="absolute"):
        load_checkout_map(checkout_map)


def test_a_member_override_wins_over_the_shared_default(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))

    gadgets = tomllib.loads(render(project, "acme/gadgets"))

    assert gadgets["engine"]["model"] == "gadget-model"
    # Overriding one key leaves its siblings alone rather than replacing the
    # whole table, or a member correcting a model would silently lose its
    # engine name too.
    assert gadgets["engine"]["name"] == "claude"
    assert gadgets["engine"]["cheap"]["model"] == "small-model"


def test_the_member_slug_reaches_the_rendered_configuration(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))

    assert tomllib.loads(render(project, "acme/widgets"))["forge"]["slug"] == "acme/widgets"


def test_rendering_is_deterministic(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))

    assert render(project, "acme/widgets") == render(project, "acme/widgets")


def test_an_unknown_member_is_rejected(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))

    with pytest.raises(FleetError, match=r"acme/absent"):
        render(project, "acme/absent")


def test_a_member_taking_an_undeclared_loop_is_rejected(tmp_path: Path) -> None:
    body = PROJECT.replace('loops = ["hardcode"]', 'loops = ["hardcode", "absent"]')

    with pytest.raises(FleetError, match="absent"):
        load_project(_project(tmp_path, body))


def test_the_fleet_may_not_own_validation_gate_authorization(tmp_path: Path) -> None:
    body = PROJECT + '\n[defaults.target.root]\npath = "."\n'

    with pytest.raises(FleetError, match="target"):
        load_project(_project(tmp_path, body))


def test_the_fleet_may_not_name_the_generated_file(tmp_path: Path) -> None:
    body = PROJECT.replace(
        "[defaults]\ntimezone", '[defaults]\ngenerated = "elsewhere.toml"\ntimezone'
    )

    with pytest.raises(FleetError, match="generated"):
        load_project(_project(tmp_path, body))


def test_a_credential_value_never_survives_rendering(tmp_path: Path) -> None:
    body = PROJECT.replace(
        "[defaults.engine.cheap]", '[defaults.engine.cheap]\napi_key_ref = "sk-live-actual-value"'
    )

    with pytest.raises(FleetError, match="api_key_ref"):
        load_project(_project(tmp_path, body))


def test_an_op_reference_is_carried_through(tmp_path: Path) -> None:
    body = PROJECT.replace(
        "[defaults.engine.cheap]",
        "[defaults.engine.cheap]\n"
        'api_key_ref = "op://01 Personal Development/anthropic/credential"',
    )
    project = load_project(_project(tmp_path, body))

    rendered = tomllib.loads(render(project, "acme/widgets"))

    assert rendered["engine"]["cheap"]["api_key_ref"].startswith("op://")


def test_sync_check_reports_a_member_whose_file_is_absent(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))
    (tmp_path / "widgets").mkdir()
    (tmp_path / "gadgets").mkdir()

    report = sync_check(project)

    assert report.drifted == ("acme/widgets", "acme/gadgets")
    assert report.exit_code == 3


def test_sync_check_is_silent_when_every_member_matches(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))
    for slug, directory in (("acme/widgets", "widgets"), ("acme/gadgets", "gadgets")):
        target = tmp_path / directory / ".touchstone" / "fleet.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(project, slug), encoding="utf-8")

    report = sync_check(project)

    assert report.drifted == ()
    assert report.exit_code == 0


def test_sync_check_never_writes(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))
    (tmp_path / "widgets").mkdir()
    (tmp_path / "gadgets").mkdir()

    sync_check(project)

    assert not (tmp_path / "widgets" / ".touchstone").exists()


_GENERATED = """
[metadata]
package_version = "0.1.2"
source_digest = "sha256:example"

[target.root]
path = "."
profiles = ["generic"]
"""

_FLEET = """
timezone = "Australia/Sydney"

[engine]
name = "claude"
model = "fleet-model"

[loop.hardcode]
brief = "builtin:hardcode"
label = "touchstone:hardcode"
schedule = "daily@03:00"
"""


def _member_repo(tmp_path: Path, project_body: str) -> Path:
    (tmp_path / ".touchstone").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".touchstone" / "generated.toml").write_text(_GENERATED, encoding="utf-8")
    (tmp_path / ".touchstone" / "fleet.toml").write_text(_FLEET, encoding="utf-8")
    root = tmp_path / "touchstone.toml"
    root.write_text(project_body, encoding="utf-8")
    return root


def test_a_repository_extends_the_fleet_fragment(tmp_path: Path) -> None:
    from touchstone.config import load

    root = _member_repo(
        tmp_path,
        "\n".join(
            [
                "version = 2",
                'generated = ".touchstone/generated.toml"',
                'extends = ".touchstone/fleet.toml"',
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
            ]
        ),
    )

    config = load(root)

    assert config.engine.model == "fleet-model"
    assert config.timezone == "Australia/Sydney"
    assert set(config.loops) == {"hardcode"}


def test_a_repository_key_wins_over_the_fleet_key(tmp_path: Path) -> None:
    from touchstone.config import load

    root = _member_repo(
        tmp_path,
        "\n".join(
            [
                "version = 2",
                'generated = ".touchstone/generated.toml"',
                'extends = ".touchstone/fleet.toml"',
                'timezone = "UTC"',
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
                "[engine]",
                'model = "local-model"',
            ]
        ),
    )

    config = load(root)

    # The fleet proposes; the repository disposes. A member that disagrees says
    # so in its own file rather than being unable to.
    assert config.engine.model == "local-model"
    assert config.engine.name == "claude"
    assert config.timezone == "UTC"


def test_an_extends_path_may_not_escape_the_repository(tmp_path: Path) -> None:
    from touchstone.config import ConfigError, load

    root = _member_repo(
        tmp_path,
        "\n".join(
            [
                "version = 2",
                'generated = ".touchstone/generated.toml"',
                'extends = "../outside.toml"',
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
            ]
        ),
    )

    with pytest.raises(ConfigError, match="extends"):
        load(root)


def test_a_missing_fleet_fragment_is_named(tmp_path: Path) -> None:
    from touchstone.config import ConfigError, load

    root = _member_repo(
        tmp_path,
        "\n".join(
            [
                "version = 2",
                'generated = ".touchstone/generated.toml"',
                'extends = ".touchstone/absent.toml"',
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
            ]
        ),
    )

    with pytest.raises(ConfigError, match=r"absent\.toml"):
        load(root)


def test_a_rendered_fragment_loads_as_a_repository_extends_it(tmp_path: Path) -> None:
    from touchstone.config import load

    project = load_project(_project(tmp_path))
    repository = tmp_path / "widgets"
    (repository / ".touchstone").mkdir(parents=True)
    (repository / ".touchstone" / "generated.toml").write_text(_GENERATED, encoding="utf-8")
    (repository / ".touchstone" / "fleet.toml").write_text(
        render(project, "acme/widgets"), encoding="utf-8"
    )
    root = repository / "touchstone.toml"
    root.write_text(
        "\n".join(
            [
                "version = 2",
                'generated = ".touchstone/generated.toml"',
                'extends = ".touchstone/fleet.toml"',
                "[project]",
                'path = "."',
            ]
        ),
        encoding="utf-8",
    )

    config = load(root)

    assert config.forge.slug == "acme/widgets"
    assert set(config.loops) == {"hardcode", "naming"}
    assert config.engine_for("hardcode").model == "small-model"


def test_sync_proposes_a_branch_and_never_writes_to_the_default(tmp_path: Path) -> None:
    from touchstone.execution.base import Result
    from touchstone.fleet import sync_propose

    class ExecutorStub:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(list(argv))
            return Result(0, "", "")

    project = load_project(_project(tmp_path))
    for directory in ("widgets", "gadgets"):
        (tmp_path / directory / ".git").mkdir(parents=True)
    executor = ExecutorStub()

    report = sync_propose(project, executor, branch_prefix="touchstone/fleet-")

    assert {slug for slug, _ in report.proposed} == {"acme/widgets", "acme/gadgets"}
    assert report.failed == ()
    # A branch and a pull request, never a push to the default branch.
    assert any(
        call[:4] == ["git", "-C", str(tmp_path / "widgets"), "checkout"] for call in executor.calls
    )
    assert any(call[:3] == ["gh", "pr", "create"] for call in executor.calls)
    pushes = [call for call in executor.calls if "push" in call]
    assert all(any(item.startswith("touchstone/fleet-") for item in call) for call in pushes)


def test_an_unchanged_member_is_not_proposed(tmp_path: Path) -> None:
    from touchstone.execution.base import Result
    from touchstone.fleet import sync_propose

    class ExecutorStub:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(list(argv))
            return Result(0, "", "")

    project = load_project(_project(tmp_path))
    for slug, directory in (("acme/widgets", "widgets"), ("acme/gadgets", "gadgets")):
        (tmp_path / directory / ".git").mkdir(parents=True)
        target = tmp_path / directory / ".touchstone" / "fleet.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(project, slug), encoding="utf-8")
    executor = ExecutorStub()

    report = sync_propose(project, executor, branch_prefix="touchstone/fleet-")

    assert report.proposed == ()
    assert set(report.unchanged) == {"acme/widgets", "acme/gadgets"}
    assert executor.calls == []


def test_a_member_that_is_not_a_checkout_is_reported_not_created(tmp_path: Path) -> None:
    from touchstone.fleet import sync_propose

    class ExecutorStub:
        def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("no command should run for a missing checkout")

    project = load_project(_project(tmp_path))

    report = sync_propose(project, ExecutorStub(), branch_prefix="touchstone/fleet-")

    assert {slug for slug, _ in report.failed} == {"acme/widgets", "acme/gadgets"}
    assert report.exit_code == 1
