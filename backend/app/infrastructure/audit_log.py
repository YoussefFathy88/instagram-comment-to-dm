"""Append-only JSONL audit log (logs/engagement.jsonl). Never rewrites old lines."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.infrastructure.config import Redactor


class JsonlAuditLog:
    def __init__(self, path: Path, redact: Redactor):
        self.path = path
        self.redact = redact

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(self.redact(line) + "\n")

    def recent(self, event: str, limit: int, **match: Any) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        found: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if f'"{event}"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == event and all(row.get(k) == v for k, v in match.items()):
                    found.append(row)
        return found[::-1][:limit]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
