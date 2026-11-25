"""In-memory fakes for the domain ports. They record every side effect so tests can assert on them."""

from datetime import datetime, timedelta, timezone
from typing import Any

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = NOW):
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, **kw) -> None:
        self.current += timedelta(**kw)


class FakeAudit:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def append(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def recent(self, event: str, limit: int, **match: Any) -> list[dict[str, Any]]:
        rows = [e for e in self.events if e.get("event") == event and all(e.get(k) == v for k, v in match.items())]
        return rows[::-1][:limit]
