from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.config import ConfigError, load_config
from touchstone.migrate import apply_v2_migration, migrate_config, preview_v2_migration

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


def test_v2_migration_preview_is_read_only_and_apply_is_backup_first(tmp_path: Path) -> None:
    source = tmp_path / "touchstone.toml"
    source.write_text(
        """\
version = 1
[project]
path = "."
[forge]
slug = "acme/widgets"
[engine]
name = "codex"
model = "gpt-test"
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
schedule = "hourly"
""",
        encoding="utf-8",
    )
    original = source.read_text(encoding="utf-8")

    preview = preview_v2_migration(source, timezone="UTC", hourly_minute=15)

    assert source.read_text(encoding="utf-8") == original
    assert not preview.generated_path.exists()
    assert "version = 2" in preview.root_text
    assert 'schedule = "hourly@15"' in preview.root_text
    assert preview.warnings

    report = apply_v2_migration(preview)

    assert report.backup.read_text(encoding="utf-8") == original
    assert report.generated == tmp_path / ".touchstone/generated.toml"
    assert report.generated.exists()
    assert load_config(source).source.schema_version == 2
