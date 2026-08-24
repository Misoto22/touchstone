"""What the loop remembers between runs.

One JSON object per line, outside the repository on purpose: a ledger
committed to the repo would be rewritten by the loop's own pull requests and
conflict with itself.

The `handled` filter is an allowlist and the distinction is not academic. Only
two statuses mean a finding has somewhere to live — `merging`, where a pull
request is queued, and `escalated`, where a draft waits for a person. Every
other outcome leaves the defect exactly where it was, and feeding those titles
back as "already handled" hides a defect nobody fixed. Written the other way
round first, as a denylist of one status, it did precisely that.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

#: Statuses that dispose of a finding. Everything else is re-raisable.
HANDLED = frozenset({"merging", "escalated"})


class Ledger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def record(self, **fields: Any) -> None:
        row = {"ts": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A truncated write is a lost record, not a lost run.
                continue
        return rows

    def handled_titles(self) -> list[str]:
        """Findings that already have a pull request, newest wording kept once."""
        seen: dict[str, None] = {}
        for row in self.rows():
            title = row.get("title") or ""
            if title and row.get("status") in HANDLED:
                seen[f"[{row['status']}/{row.get('risk', 'n/a')}] {title}"] = None
        return list(seen)
