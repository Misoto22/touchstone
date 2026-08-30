from __future__ import annotations

import json
import tomllib
from pathlib import Path

from touchstone.cli import main
from touchstone.harnesses import HarnessContext
from touchstone.inspection import configuration_paths, effective_configuration


def _configured_repository(tmp_path: Path, *, entrypoint: bool = True) -> Path:
    (tmp_path / ".touchstone").mkdir()
    (tmp_path / ".touchstone/generated.toml").write_text(
        """\
[metadata]
package_version = "0.1.2"
source_digest = "sha256:test"
[metadata.profile_versions]
""",
        encoding="utf-8",
    )
    (tmp_path / ".touchstone/fleet.toml").write_text(
        """\
[engine]
name = "codex"
model = "gpt-test"
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
""",
        encoding="utf-8",
    )
    config = tmp_path / "touchstone.toml"
    config.write_text(
        """\
version = 2
generated = ".touchstone/generated.toml"
extends = ".touchstone/fleet.toml"
timezone = "Australia/Sydney"
[project]
path = "."
[forge]
slug = "acme/widgets"
[harness]
mode = "embedded"
entrypoint = "AGENTS.md"
""",
        encoding="utf-8",
    )
    if entrypoint:
        (tmp_path / "AGENTS.md").write_text("repository rules\n", encoding="utf-8")
    return config


def test_configuration_paths_names_each_owner(tmp_path: Path) -> None:
    paths = configuration_paths(_configured_repository(tmp_path))

    assert paths["project"] == (tmp_path / "touchstone.toml").resolve()
    assert paths["generated"] == (tmp_path / ".touchstone/generated.toml").resolve()
    assert paths["fleet"] == (tmp_path / ".touchstone/fleet.toml").resolve()
    assert paths["harness_registry"].name == "harnesses.toml"


def test_effective_configuration_reports_field_owner(tmp_path: Path) -> None:
    effective = effective_configuration(_configured_repository(tmp_path))

    assert effective["harness.mode"] == {
        "value": "embedded",
        "source": "touchstone.toml",
    }
    assert effective["engine.model"] == {
        "value": "gpt-test",
        "source": ".touchstone/fleet.toml",
    }
    assert effective["metadata.source_digest"] == {
        "value": "sha256:test",
        "source": ".touchstone/generated.toml",
    }


def test_effective_configuration_reports_environment_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)
    monkeypatch.setenv("TOUCHSTONE_MODEL", "gpt-override")

    effective = effective_configuration(config)

    assert effective["engine.model"] == {
        "value": "gpt-override",
        "source": "environment:TOUCHSTONE_MODEL",
    }


def test_config_show_effective_json_uses_public_boundary(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "show", "--effective", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["harness.mode"]["value"] == "embedded"


def test_config_check_returns_typed_blocked_result_for_missing_harness_entrypoint(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path, entrypoint=False)

    assert main(["--config", str(config), "config", "check", "--json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "blocked",
        "reason": "harness-entrypoint-missing",
        "detail": "Harness entrypoint does not exist: AGENTS.md",
    }


def test_harness_resolve_json_does_not_expose_machine_snapshot_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "harness", "resolve", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["entrypoint"] == "AGENTS.md"
    assert str(tmp_path) not in json.dumps(payload)


def test_harness_resolve_removes_external_snapshot_after_inspection(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)
    snapshot = tmp_path / "snapshot-test"
    entrypoint = snapshot / "harness/00-INDEX.md"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("rules\n", encoding="utf-8")
    context = HarnessContext(
        mode="external",
        source="acme/harness",
        entrypoint=entrypoint,
        revision="abc123",
        context_root=snapshot,
        evidence=("ref:origin/main",),
    )
    monkeypatch.setattr("touchstone.harnesses.resolve_harness", lambda *_args, **_kwargs: context)

    assert main(["--config", str(config), "harness", "resolve", "--json"]) == 0
    capsys.readouterr()

    assert not snapshot.exists()


def test_harness_registry_commands_mutate_only_the_local_registry(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    xdg = tmp_path / "xdg"
    checkout = tmp_path / "efficient-harness"
    checkout.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert (
        main(
            [
                "harness",
                "register",
                "Efficient-Pty-Ltd/efficient-harness",
                "--path",
                str(checkout),
            ]
        )
        == 0
    )
    registry = xdg / "touchstone/harnesses.toml"
    raw = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert raw == {
        "harnesses": {"Efficient-Pty-Ltd/efficient-harness": {"path": str(checkout.resolve())}}
    }
    assert main(["harness", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert listed == {"Efficient-Pty-Ltd/efficient-harness": str(checkout.resolve())}

    assert main(["harness", "unregister", "Efficient-Pty-Ltd/efficient-harness"]) == 0
    assert tomllib.loads(registry.read_text(encoding="utf-8")) == {"harnesses": {}}


def test_config_explain_reports_one_effective_field(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _configured_repository(tmp_path)

    assert main(["--config", str(config), "config", "explain", "harness.mode", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "field": "harness.mode",
        "value": "embedded",
        "source": "touchstone.toml",
    }
