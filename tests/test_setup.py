from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_config import _valid_config, _write
from touchstone.config import ConfigError, load_config
from touchstone.setup import setup


class MemoryForge:
    def __init__(self) -> None:
        self._labels: set[str] = set()

    def labels(self) -> set[str]:
        return set(self._labels)

    def ensure_label(self, name: str, *, color: str, description: str) -> bool:
        changed = name not in self._labels
        self._labels.add(name)
        return changed


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _valid_config().replace('path = "."', 'path = "repo"')
    text = text.replace("version = 1", 'version = 1\nstate_dir = "state"')
    return load_config(_write(tmp_path / "touchstone.toml", text))


def test_setup_dry_run_reports_labels_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    forge = MemoryForge()

    report = setup(config, dry_run=True, forge=forge)

    assert report.planned_labels == ("touchstone:audit", "touchstone:needs-review")
    assert forge.labels() == set()
    assert not config.state_dir.exists()


def test_setup_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    forge = MemoryForge()

    first = setup(config, dry_run=False, forge=forge)
    second = setup(config, dry_run=False, forge=forge)

    assert first.created_labels == ("touchstone:audit", "touchstone:needs-review")
    assert second.created_labels == ()
    assert config.state_dir.is_dir()


def test_setup_fails_when_a_label_cannot_be_created(tmp_path: Path) -> None:
    class FailingForge(MemoryForge):
        def ensure_label(self, name: str, *, color: str, description: str) -> bool:
            return False

    with pytest.raises(ConfigError, match="could not create GitHub label"):
        setup(_config(tmp_path), dry_run=False, forge=FailingForge())
