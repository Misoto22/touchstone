"""The forge answers Analysis needs and cannot ask for itself.

The local runner refuses to buy an author session while a Loop's pull request
slot is held or the default branch is not known good. The hosted Analysis Stage
holds the model credential and no GitHub token, so it could ask neither, and
bought a session on every wake regardless: the hosted probe repository reached
91 open Touchstone pull requests where the local backend would have stopped at
one. The Preparation Stage holds the read-only workflow token and no model
credential, so it asks on Analysis's behalf and hands the answers over in its
artifact.

Facts, not verdicts. Prepare cannot decrypt state, so it does not know which
Loop is due or which parked pull request a reanalysis replaces; Analysis does,
and applies the facts to the one Loop it claimed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GATES_FILE = "forge-gates.json"
_VERSION = 1


@dataclass(frozen=True, slots=True)
class Holder:
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class ForgeGates:
    #: Why the default branch is not known good; empty when it is.
    health: str
    #: Open pull requests holding each Loop's slot. `None` for a Loop means
    #: GitHub could not say, which refuses rather than passes.
    slots: dict[str, tuple[Holder, ...] | None]

    def refusal(self, loop: str, *, excluding: int | None = None) -> tuple[str, str] | None:
        """`(reason_code, detail)` stopping this Loop, in the local gate order.

        `excluding` is the parked pull request a reanalysis replaces. It is
        still open while its successor is analyzed, and for a Loop whose drafts
        hold the slot it would otherwise block its own replacement.
        """

        holders = self.slots.get(loop)
        if holders is None:
            return ("slot-unverifiable", "could not verify the open pull request slot")
        held = [holder for holder in holders if holder.number != excluding]
        if held:
            return ("slot-held", f"slot held by #{held[0].number}: {held[0].url}")
        if self.health:
            return ("production-not-known-good", self.health)
        return None

    def write(self, path: Path) -> None:
        payload = {
            "version": _VERSION,
            "health": self.health,
            "slots": {
                loop: None
                if holders is None
                else [{"number": holder.number, "url": holder.url} for holder in holders]
                for loop, holders in sorted(self.slots.items())
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def collect(config: Any, forge: Any) -> ForgeGates:
    """Ask GitHub every question any configured Loop's gate will need."""

    from touchstone import runner

    try:
        runner._health_gate(config, forge)
    except runner.Held as held:
        health = str(held)
    else:
        health = ""
    slots: dict[str, tuple[Holder, ...] | None] = {}
    for name, loop in config.loops.items():
        pulls = forge.open_pulls(loop.label, include_drafts=loop.drafts_hold_slot)
        slots[name] = (
            None
            if pulls is None
            else tuple(Holder(int(pull["number"]), str(pull.get("url", ""))) for pull in pulls)
        )
    return ForgeGates(health, slots)


def read(path: Path) -> ForgeGates:
    """The facts Prepare wrote. Anything malformed raises `ValueError`."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        raise ValueError("forge gates have an unsupported version")
    health = payload.get("health")
    slots = payload.get("slots")
    if not isinstance(health, str) or not isinstance(slots, dict):
        raise ValueError("forge gates are malformed")
    parsed: dict[str, tuple[Holder, ...] | None] = {}
    for loop, holders in slots.items():
        if holders is None:
            parsed[str(loop)] = None
            continue
        if not isinstance(holders, list):
            raise ValueError("forge gate holders are malformed")
        parsed[str(loop)] = tuple(_holder(entry) for entry in holders)
    return ForgeGates(health, parsed)


def _holder(entry: Any) -> Holder:
    if not isinstance(entry, dict):
        raise ValueError("forge gate holder is malformed")
    number = entry.get("number")
    url = entry.get("url")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("forge gate holder number is invalid")
    if not isinstance(url, str):
        raise ValueError("forge gate holder url is invalid")
    return Holder(number, url)


__all__ = ["GATES_FILE", "ForgeGates", "Holder", "collect", "read"]
