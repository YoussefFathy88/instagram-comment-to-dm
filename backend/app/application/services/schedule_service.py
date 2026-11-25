"""Turn the hourly engagement job on/off (CLI and dashboard share this)."""

from typing import Any

from app.domain.errors import DomainError
from app.domain.ports import TaskScheduler

ALLOWED_MINUTES = (30, 60, 120, 180, 240, 360)


class ScheduleService:
    def __init__(self, scheduler: TaskScheduler):
        self.scheduler = scheduler

    def status(self) -> dict[str, Any]:
        return self.scheduler.status()

    def enable(self, every_minutes: int) -> dict[str, Any]:
        if every_minutes not in ALLOWED_MINUTES:
            raise DomainError(f"every must be one of {ALLOWED_MINUTES} minutes")
        return self.scheduler.install(every_minutes)

    def disable(self) -> dict[str, Any]:
        return self.scheduler.remove()

    def restart_after_manual_run(self) -> dict[str, Any] | None:
        """Rule: a manual live batch restarts the clock, so the next scheduled run is one
        full interval after the manual one ended. Nothing to do when the scheduler is off."""
        current = self.scheduler.status()
        every = current.get("every_minutes")
        if not current.get("installed") or not every:
            return None
        return self.scheduler.install(int(every))
