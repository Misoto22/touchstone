"""Engines paused until the provider will accept work again.

A usage limit belongs to the credential, not to the run that met it, so the
pause outlives the run: it is written to the state directory, travels in the
hosted state snapshot, and is read by the gate before the next author session
is bought. Keyed by the pool member's provider, endpoint and credential
variable, because two Loops on one member share one plan and a Loop on another
member does not.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COOLDOWN_FILE = "engine-cooldown.json"
_UNREADABLE = (
    f"the engine pause record is unreadable; remove {COOLDOWN_FILE} from the state directory"
)


@dataclass(frozen=True, slots=True)
class Pause:
    until: dt.datetime
    reason: str


def member_key(engine: Any) -> str:
    """The identity a usage limit is charged to."""

    return f"{engine.name}|{engine.base_url}|{engine.key_env}"


def active(state_dir: Path | str, engine: Any, *, now: dt.datetime) -> Pause | None:
    """The pause still in force for this pool member, or `None`.

    An unreadable file refuses rather than passes: a pause that cannot be read
    is one that may still be in force, and the cost of guessing wrong is the
    session the provider already said it would refuse.
    """

    entries = _read(Path(state_dir) / COOLDOWN_FILE)
    if entries is None:
        return Pause(now + dt.timedelta(hours=1), _UNREADABLE)
    entry = entries.get(member_key(engine))
    if not isinstance(entry, dict):
        return None
    try:
        until = dt.datetime.fromisoformat(str(entry["until"])).astimezone(dt.UTC)
        reason = str(entry["reason"])
    except (KeyError, TypeError, ValueError):
        return Pause(now + dt.timedelta(hours=1), _UNREADABLE)
    if until <= now:
        return None
    return Pause(until, reason)


def record(state_dir: Path | str, engine: Any, *, until: dt.datetime, reason: str) -> None:
    """Pause this pool member until `until`, replacing any earlier pause."""

    path = Path(state_dir) / COOLDOWN_FILE
    entries = _read(path) or {}
    entries[member_key(engine)] = {
        "until": until.astimezone(dt.UTC).isoformat(),
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def pause(config: Any, loop: str, limit: Any) -> None:
    """Pause the member a Loop runs on after its session met a usage limit.

    Without this kioku's host woke every hour for three days, met the same
    limit within a minute each time, and recorded only that a session failed,
    while the transcript it kept named the day the limit would reset.
    """

    record(config.state_dir, config.engine_for(loop), until=limit.until, reason=limit.reason)


def _read(path: Path) -> dict[str, Any] | None:
    """Entries by member, `{}` when absent, `None` when present and unreadable."""

    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["COOLDOWN_FILE", "Pause", "active", "member_key", "pause", "record"]
