from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.config import ConfigError, load_config
from touchstone.migrate import migrate_config

LEGACY = """\
repo_path = "/tmp/repository"
state_dir = "~/.local/state/touchstone"

[forge]
slug = "acme/widgets"
default_branch = "trunk"
audit_label = "old:audit"
escalation_label = "old:review"

[engine]
name = "codex"
model = "gpt-test"

[loop.code]
brief = "briefs/code-audit.md"
label = "old:audit"
protected_paths = [".github/"]

[loop.code.context]
project = "this repository"
ledger = "the ledger"
protected = "the protected paths"
rules_clause = ""
"""


def test_migration_backs_up_unversioned_config_before_replacement(tmp_path: Path) -> None:
    source = tmp_path / "touchstone.toml"
    source.write_text(LEGACY, encoding="utf-8")

    report = migrate_config(source)

    assert report.backup.read_text(encoding="utf-8") == LEGACY
    loaded = load_config(source)
    assert loaded.source.schema_version == 1
    assert loaded.repo_path == Path("/tmp/repository").resolve()
    assert loaded.forge.required_workflows == ()
    assert loaded.loop("code").brief == "builtin:code-audit"


def test_migration_refuses_to_replace_a_versioned_config(tmp_path: Path) -> None:
    source = tmp_path / "touchstone.toml"
    source.write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="already versioned"):
        migrate_config(source)
