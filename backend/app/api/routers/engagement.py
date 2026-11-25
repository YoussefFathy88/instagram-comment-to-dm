"""Comment-to-DM control board API. Thin: every route calls a service and maps to schemas."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_container, get_jobs
from app.api.jobs import JobManager
from app.api.schemas import ActionOut, CampaignOut, JobOut, JobStateOut, RunIn, RunOut, SchedulerIn
from app.bootstrap import Container
from app.infrastructure.files.run_lock import RunLocked

router = APIRouter(prefix="/api/engagement", tags=["engagement"])


def _campaign(c: Container, campaign_id: str) -> CampaignOut:
    campaign = c.engagement_service.list_one(campaign_id)
    return CampaignOut.build(campaign, c.engagement_service.status(campaign.id)["ledger"])


@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(c: Container = Depends(get_container)):
    return [_campaign(c, x.id) for x in c.engagement_service.list()]


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: str, c: Container = Depends(get_container)):
    return _campaign(c, campaign_id)


@router.get("/campaigns/{campaign_id}/actions", response_model=list[ActionOut])
def list_actions(campaign_id: str, c: Container = Depends(get_container)):
    return [ActionOut.build(a) for a in c.engagement_service.report(campaign_id)[1]]


@router.get("/campaigns/{campaign_id}/runs", response_model=list[RunOut])
def list_runs(campaign_id: str, c: Container = Depends(get_container)):
    return [RunOut.build(r) for r in c.engagement_service.runs(campaign_id)]


def _start(jobs: JobManager, c: Container, kind: str, campaign_id: str, max_actions: int | None = None) -> JobOut:
    campaign = c.engagement_service.list_one(campaign_id)
    if c.engagement_lock().path.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "Another run is in progress (scheduler or CLI). Try again when it ends.")
    try:
        return JobOut.build(jobs.start(kind, campaign.id, max_actions))
    except RunLocked as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


@router.post("/campaigns/{campaign_id}/run", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def run_batch(campaign_id: str, body: RunIn, c: Container = Depends(get_container), jobs: JobManager = Depends(get_jobs)):
    """Live batch (the owner's click is the confirmation; the campaign must be approved) or dry run."""
    campaign = c.engagement_service.list_one(campaign_id)
    if not body.dry_run and not campaign.approved:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Campaign is {campaign.status.value}, not approved.")
    return _start(jobs, c, "dry_run" if body.dry_run else "run", campaign_id, body.max_actions)


@router.post("/campaigns/{campaign_id}/sync", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def sync_comments(campaign_id: str, c: Container = Depends(get_container), jobs: JobManager = Depends(get_jobs)):
    return _start(jobs, c, "sync", campaign_id)


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: str, c: Container = Depends(get_container)):
    c.engagement_service.pause(campaign_id)
    return _campaign(c, campaign_id)


@router.post("/campaigns/{campaign_id}/approve", response_model=CampaignOut)
def approve_campaign(campaign_id: str, c: Container = Depends(get_container)):
    c.engagement_service.approve(campaign_id)
    return _campaign(c, campaign_id)


@router.get("/job", response_model=JobStateOut)
def job_state(c: Container = Depends(get_container), jobs: JobManager = Depends(get_jobs)):
    return JobStateOut(job=JobOut.build(jobs.current) if jobs.current else None, lock_active=c.engagement_lock().path.exists())


@router.get("/scheduler")
def scheduler_status(c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.schedule_service.status()


@router.post("/scheduler")
def scheduler_set(body: SchedulerIn, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.schedule_service.enable(body.every_minutes) if body.enabled else c.schedule_service.disable()
