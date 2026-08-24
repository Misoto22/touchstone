"""Idempotent, explicit first-run mutations."""

from __future__ import annotations

from dataclasses import dataclass

from touchstone import execution
from touchstone.config import Config
from touchstone.forge import Forge


@dataclass(frozen=True, slots=True)
class SetupReport:
    planned_labels: tuple[str, ...]
    created_labels: tuple[str, ...]
    state_dir_created: bool


def setup(config: Config, *, dry_run: bool, forge: Forge | None = None) -> SetupReport:
    target_forge = forge or Forge(config.forge.slug, execution.build(config))
    configured = [*(loop.label for loop in config.loops.values()), config.forge.escalation_label]
    labels = tuple(dict.fromkeys(configured))
    if dry_run:
        return SetupReport(labels, (), False)

    state_dir_created = not config.state_dir.exists()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    existing = target_forge.labels()
    created: list[str] = []
    for label in labels:
        if label in existing:
            continue
        if target_forge.ensure_label(
            label,
            color="1d76db" if label != config.forge.escalation_label else "b60205",
            description="Managed by Touchstone",
        ):
            created.append(label)
    return SetupReport(labels, tuple(created), state_dir_created)


__all__ = ["SetupReport", "setup"]
