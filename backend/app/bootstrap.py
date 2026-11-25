"""Composition root: wires adapters into services. Used by the CLI and by the FastAPI deps."""

from functools import cached_property
from pathlib import Path

from app.application.services.engagement_service import EngagementService, EngagementSettings
from app.application.services.schedule_service import ScheduleService
from app.domain.errors import ConfigMissing
from app.infrastructure.audit_log import JsonlAuditLog, SystemClock
from app.infrastructure.config import PROJECT_ROOT, Redactor, Settings
from app.infrastructure.db.engine import make_session_factory
from app.infrastructure.db.repositories import SqlCampaignRepository, SqlEngagementActionRepository
from app.infrastructure.files.run_lock import RunLock
from app.infrastructure.instagram.engagement_graph import InstagramGraphEngagement
from app.infrastructure.reports.engagement_report import write_report
from app.infrastructure.scheduler import WindowsTaskScheduler


class Container:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.redact = Redactor(self.settings.secret_values())
        self.clock = SystemClock()

    # ---------------------------------------------------------------- adapters

    @cached_property
    def sessions(self):
        return make_session_factory(self.settings.path(self.settings.database_path))

    @cached_property
    def audit(self):
        return JsonlAuditLog(self.settings.path(self.settings.audit_log_path), self.redact)

    @cached_property
    def instagram_engagement(self) -> InstagramGraphEngagement:
        s = self.settings
        missing = s.missing("instagram_engage")
        if missing:
            raise ConfigMissing(missing)
        return InstagramGraphEngagement(
            ig_user_id=s.instagram_user_id,
            token=s.instagram_access_token.get_secret_value(),
            graph_version=s.meta_graph_version,
            redact=self.redact,
            host=s.instagram_graph_host,
            max_calls=s.engage_max_calls_per_run,
        )

    def engagement_lock(self) -> RunLock:
        return RunLock(self.settings.path(self.settings.database_path).parent / "engagement.lock")

    # ---------------------------------------------------------------- services

    @cached_property
    def engagement_service(self) -> EngagementService:
        s = self.settings
        return EngagementService(
            campaigns=SqlCampaignRepository(self.sessions),
            actions=SqlEngagementActionRepository(self.sessions),
            instagram=lambda: self.instagram_engagement,
            audit=self.audit,
            clock=self.clock,
            settings=EngagementSettings(delay_s=(s.engage_delay_min_s, s.engage_delay_max_s)),
        )

    @cached_property
    def schedule_service(self) -> ScheduleService:
        return ScheduleService(WindowsTaskScheduler(PROJECT_ROOT / "scripts" / "engage-run.cmd"))

    def write_engagement_report(self, campaign_id: str) -> Path:
        """Rewrite data/reports/<campaign>.html from the database."""
        campaign, actions = self.engagement_service.report(campaign_id)
        out_dir = self.settings.path(self.settings.database_path).parent / "reports"
        return write_report(campaign, actions, out_dir, self.settings.timezone, self.clock.now())
