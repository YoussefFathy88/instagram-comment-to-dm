"""Background jobs for the dashboard: one Instagram job at a time (batch, dry run, or sync).

Each job gets a fresh Container, so it reads the current .env (e.g. a refreshed token) and gets its own
per-run API call budget. The engine's lock file also blocks overlap with the scheduled task.
"""

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.bootstrap import Container
from app.domain.errors import DomainError
from app.infrastructure.files.run_lock import RunLocked


@dataclass
class Job:
    id: str
    kind: str  # run | dry_run | sync
    campaign: str
    max_actions: int | None
    started_at: datetime
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None


@dataclass
class JobManager:
    container_factory: Callable[[], Container] = Container
    current: Job | None = None
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def busy(self) -> bool:
        return bool(self.current and self.current.running)

    def start(self, kind: str, campaign: str, max_actions: int | None = None) -> Job:
        with self._guard:
            if self.busy():
                raise RunLocked("A job from this dashboard is still running.")
            job = Job(secrets.token_hex(4), kind, campaign, max_actions, datetime.now(timezone.utc))
            self.current = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"engage-{job.id}").start()
        return job

    def _run(self, job: Job) -> None:
        c = self.container_factory()
        try:
            with c.engagement_lock():
                service = c.engagement_service
                if job.kind == "sync":
                    report = service.sync(job.campaign)
                else:
                    report = service.run(job.campaign, dry_run=job.kind == "dry_run", max_actions=job.max_actions, trigger="board")
            job.result = report.to_dict()
            if job.kind != "dry_run":
                c.write_engagement_report(job.campaign)  # same HTML report the CLI writes
            if job.kind == "run":
                job.result["scheduler"] = self._restart_schedule(c)
        except DomainError as e:
            job.error = c.redact(str(e))
        except Exception as e:  # never leave a job "running" forever
            job.error = c.redact(f"{type(e).__name__}: {e}")
        finally:
            job.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _restart_schedule(c: Container) -> dict[str, Any] | str | None:
        """A manual batch restarts the scheduler clock (next run = one interval after it). Never fails the job."""
        try:
            return c.schedule_service.restart_after_manual_run()
        except Exception as e:
            return c.redact(f"couldn't restart the scheduler clock: {e}")
