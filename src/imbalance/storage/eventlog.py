"""Append-only event log (spec §11): one JSON line per state transition, alert,
trade update or status change, with every input value used. Records carry
exchange/clock time only, so replaying a recorded day twice yields identical
files."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

from ..engine import Event


def event_line(e: Event, config_version: str | None = None) -> str:
    rec = e.to_record()
    if config_version is not None:
        rec["config_version"] = config_version
    return json.dumps(rec, sort_keys=True, separators=(",", ":"))


class EventLogWriter:
    def __init__(self, directory: str | Path, day: date, config_version: str | None = None,
                 name: str | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / (name or f"{day.isoformat()}.jsonl")
        self.config_version = config_version
        self._fh = self.path.open("a", encoding="utf-8")
        self.count = 0

    def write(self, events: Iterable[Event]) -> None:
        for e in events:
            self._fh.write(event_line(e, self.config_version) + "\n")
            self.count += 1
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def read_event_log(path: str | Path) -> list[dict]:
    out = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
