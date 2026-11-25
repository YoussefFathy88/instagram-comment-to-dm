"""API response/request models. The API never returns ORM objects or domain objects directly."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.api.jobs import Job
from app.domain.engagement import Campaign, EngagementAction


class CampaignOut(BaseModel):
    id: str
    name: str
    status: str
    approved: bool
    media_links: dict[str, str]
    keywords: list[str]
    dm_message: str
    replies: list[str]
    max_actions_per_run: int
    ledger: dict[str, int]

    @classmethod
    def build(cls, c: Campaign, ledger: dict[str, int]) -> "CampaignOut":
        return cls(
            id=c.id,
            name=c.name,
            status=c.status.value,
            approved=c.approved,
            media_links={m: c.media_links.get(m, "") for m in c.media_ids},
            keywords=c.keywords,
            dm_message=c.dm_message,
            replies=c.replies,
            max_actions_per_run=c.max_actions_per_run,
            ledger=ledger,
        )


class ActionOut(BaseModel):
    comment_id: str
    media_id: str
    username: str
    comment: str
    comment_at: datetime
    state: str
    dm_message_id: str | None
    reply: str | None
    error: str | None
    updated_at: datetime | None

    @classmethod
    def build(cls, a: EngagementAction) -> "ActionOut":
        return cls(
            comment_id=a.comment_id,
            media_id=a.media_id,
            username=a.username,
            comment=a.comment_text,
            comment_at=a.comment_at,
            state=a.state.value,
            dm_message_id=a.dm_message_id,
            reply=a.reply_text,
            error=a.error,
            updated_at=a.updated_at,
        )


class RunIn(BaseModel):
    max_actions: int | None = Field(default=None, ge=1, le=200)
    dry_run: bool = False


class JobOut(BaseModel):
    id: str
    kind: str
    campaign: str
    max_actions: int | None
    running: bool
    started_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: str | None

    @classmethod
    def build(cls, j: Job) -> "JobOut":
        return cls(
            id=j.id,
            kind=j.kind,
            campaign=j.campaign,
            max_actions=j.max_actions,
            running=j.running,
            started_at=j.started_at,
            finished_at=j.finished_at,
            result=j.result,
            error=j.error,
        )


class RunOut(BaseModel):
    """One finished live run, from the audit log (logs/engagement.jsonl)."""

    at: str
    trigger: str  # scheduler | board | cli
    ok: bool
    counts: dict[str, int]
    deferred: int
    recovered: int
    aborted: str | None  # the stop reason (root cause)
    warnings: list[str]
    api_calls: int | None
    usage_pct: float | None

    @classmethod
    def build(cls, r: dict[str, Any]) -> "RunOut":
        return cls(
            at=r.get("at", ""),
            trigger=r.get("trigger", "cli"),
            ok=bool(r.get("ok")),
            counts=r.get("counts") or {},
            deferred=r.get("deferred") or 0,
            recovered=r.get("recovered") or 0,
            aborted=r.get("aborted"),
            warnings=r.get("warnings") or [],
            api_calls=r.get("api_calls"),
            usage_pct=r.get("usage_pct"),
        )


class JobStateOut(BaseModel):
    job: JobOut | None
    lock_active: bool  # a run holds the engine lock (this dashboard, the scheduler, or the CLI)


class SchedulerIn(BaseModel):
    enabled: bool
    every_minutes: int = 60
