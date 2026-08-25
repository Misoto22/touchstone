from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.cli import main
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


def test_v2_migration_cli_previews_then_requires_explicit_write(tmp_path: Path) -> None:
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

    assert main(["config", "migrate-v2", str(source), "--check"]) == 3
    assert "version = 1" in source.read_text(encoding="utf-8")
    assert main(["config", "migrate-v2", str(source), "--write"]) == 0
    assert load_config(source).source.schema_version == 2


def test_migration_keeps_a_v1_harness_review_holding_its_slot_against_drafts(
    tmp_path: Path,
) -> None:
    # v1 read this off `require_change_under`, which the harness review was
    # alone in setting. The code audit keeps the default: it parks medium-risk
    # findings as drafts and has to stay runnable.
    source = tmp_path / "touchstone.toml"
    source.write_text(
        LEGACY
        + """
[loop.harness]
brief = "briefs/harness-review.md"
label = "old:harness"
require_change_under = ["docs/engineering/"]
confine_to = ["docs/engineering/"]

[loop.harness.context]
project = "the harness"
""",
        encoding="utf-8",
    )

    migrate_config(source)
    loaded = load_config(source)

    assert loaded.loop("harness").brief == "builtin:harness-review"
    assert loaded.loop("harness").drafts_hold_slot is True
    assert loaded.loop("code").drafts_hold_slot is False
