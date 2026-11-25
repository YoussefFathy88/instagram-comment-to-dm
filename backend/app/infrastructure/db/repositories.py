"""SQLAlchemy implementations of the repository ports. The only code that touches the ORM."""

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.engagement import ActionState, Campaign, CampaignStatus, EngagementAction
from app.domain.errors import CampaignNotFound
from app.infrastructure.db.orm_models import CampaignRow, EngagementActionRow


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; everything is stored as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_CAMPAIGN_FIELDS = ("media_ids", "media_links", "keywords", "dm_message", "replies", "fallback_reply", "unverified_prefix", "scan_limit", "max_actions_per_run", "history_check")


class SqlCampaignRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    @staticmethod
    def _to_campaign(row: CampaignRow) -> Campaign:
        return Campaign(
            id=row.id,
            name=row.name,
            status=CampaignStatus(row.status),
            approved_fingerprint=row.approved_fingerprint,
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
            **{k: row.payload[k] for k in _CAMPAIGN_FIELDS if k in row.payload},
        )

    def save(self, campaign: Campaign) -> Campaign:
        now = _now()
        with self.sessions.begin() as s:
            row = s.merge(
                CampaignRow(
                    id=campaign.id,
                    name=campaign.name,
                    status=campaign.status.value,
                    approved_fingerprint=campaign.approved_fingerprint,
                    payload={k: getattr(campaign, k) for k in _CAMPAIGN_FIELDS},
                    created_at=campaign.created_at or now,
                    updated_at=campaign.updated_at or now,
                )
            )
            s.flush()
            return self._to_campaign(row)

    def get(self, campaign_id: str) -> Campaign:
        with self.sessions() as s:
            row = s.get(CampaignRow, campaign_id) or s.scalar(select(CampaignRow).where(CampaignRow.name == campaign_id))
            if row is None:
                raise CampaignNotFound(f"Campaign {campaign_id} not found.")
            return self._to_campaign(row)

    def get_by_name(self, name: str) -> Campaign | None:
        with self.sessions() as s:
            row = s.scalar(select(CampaignRow).where(CampaignRow.name == name))
            return self._to_campaign(row) if row else None

    def list(self) -> list[Campaign]:
        with self.sessions() as s:
            return [self._to_campaign(r) for r in s.scalars(select(CampaignRow).order_by(CampaignRow.created_at))]


_OPEN = [ActionState.QUEUED.value, ActionState.NO_KEYWORD.value]

# States in which the user has (or already had, outside the tool) our DM.
_DM_STATES = [ActionState.DONE.value, ActionState.DM_ONLY.value, ActionState.SKIPPED_HISTORY.value, ActionState.PENDING.value]


class SqlEngagementActionRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    @staticmethod
    def _to_action(row: EngagementActionRow) -> EngagementAction:
        return EngagementAction(
            id=row.id,
            campaign_id=row.campaign_id,
            comment_id=row.comment_id,
            media_id=row.media_id,
            user_id=row.user_id,
            username=row.username,
            comment_text=row.comment_text,
            comment_at=_utc(row.comment_at),
            state=ActionState(row.state),
            dm_message_id=row.dm_message_id,
            reply_text=row.reply_text,
            reply_comment_id=row.reply_comment_id,
            error=row.error,
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
        )

    def claim(self, action: EngagementAction) -> bool:
        now = _now()
        with self.sessions.begin() as s:
            row = s.scalar(self._row_query(action.campaign_id, action.comment_id))
            if row is not None:
                if row.state not in _OPEN:
                    return False
                row.state, row.error, row.updated_at = action.state.value, action.error, now
                s.flush()
                action.id = row.id
                return True
        try:
            with self.sessions.begin() as s:
                row = EngagementActionRow(
                    campaign_id=action.campaign_id,
                    comment_id=action.comment_id,
                    media_id=action.media_id,
                    user_id=action.user_id,
                    username=action.username,
                    comment_text=action.comment_text,
                    comment_at=action.comment_at,
                    state=action.state.value,
                    error=action.error,
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
                s.flush()
                action.id = row.id
        except IntegrityError:
            return False
        return True

    @staticmethod
    def _row_query(campaign_id: str, comment_id: str):
        return select(EngagementActionRow).where(
            EngagementActionRow.campaign_id == campaign_id, EngagementActionRow.comment_id == comment_id
        )

    def sync(self, actions: list[EngagementAction]) -> None:
        now = _now()
        with self.sessions.begin() as s:
            existing = {
                r.comment_id: r
                for r in s.scalars(
                    select(EngagementActionRow).where(
                        EngagementActionRow.campaign_id.in_({a.campaign_id for a in actions}),
                        EngagementActionRow.comment_id.in_([a.comment_id for a in actions]),
                    )
                )
            } if actions else {}
            for a in actions:
                row = existing.get(a.comment_id)
                if row is None:
                    s.add(EngagementActionRow(
                        campaign_id=a.campaign_id, comment_id=a.comment_id, media_id=a.media_id, user_id=a.user_id,
                        username=a.username, comment_text=a.comment_text, comment_at=a.comment_at, state=a.state.value,
                        error=a.error, created_at=now, updated_at=now,
                    ))
                elif row.state in _OPEN and (row.state != a.state.value or row.comment_text != a.comment_text):
                    row.state, row.comment_text, row.updated_at = a.state.value, a.comment_text, now

    def update(self, action: EngagementAction) -> None:
        with self.sessions.begin() as s:
            row = s.scalar(
                select(EngagementActionRow).where(
                    EngagementActionRow.campaign_id == action.campaign_id,
                    EngagementActionRow.comment_id == action.comment_id,
                )
            )
            if row is None:
                return
            row.state = action.state.value
            row.dm_message_id = action.dm_message_id
            row.reply_text = action.reply_text
            row.reply_comment_id = action.reply_comment_id
            row.error = action.error
            row.updated_at = _now()

    def handled_comment_ids(self, campaign_id: str) -> set[str]:
        with self.sessions() as s:
            q = select(EngagementActionRow.comment_id).where(
                EngagementActionRow.campaign_id == campaign_id, EngagementActionRow.state.not_in(_OPEN)
            )
            return set(s.scalars(q))

    def users_with_dm(self, campaign_id: str) -> set[str]:
        with self.sessions() as s:
            q = select(EngagementActionRow.user_id).where(
                EngagementActionRow.campaign_id == campaign_id, EngagementActionRow.state.in_(_DM_STATES)
            )
            return set(s.scalars(q))

    def counts(self, campaign_id: str) -> dict[str, int]:
        with self.sessions() as s:
            q = (
                select(EngagementActionRow.state, func.count())
                .where(EngagementActionRow.campaign_id == campaign_id)
                .group_by(EngagementActionRow.state)
            )
            return {state: n for state, n in s.execute(q)}

    def forget_user(self, username: str) -> int:
        with self.sessions.begin() as s:
            result = s.execute(delete(EngagementActionRow).where(func.lower(EngagementActionRow.username) == username.lower()))
            return result.rowcount or 0

    def recent_replies(self, campaign_id: str, limit: int) -> list[str]:
        """Newest first."""
        with self.sessions() as s:
            q = (
                select(EngagementActionRow.reply_text)
                .where(EngagementActionRow.campaign_id == campaign_id, EngagementActionRow.reply_text.is_not(None))
                .order_by(EngagementActionRow.id.desc())
                .limit(limit)
            )
            return list(s.scalars(q))

    def list(self, campaign_id: str, limit: int = 200) -> list[EngagementAction]:
        with self.sessions() as s:
            q = (
                select(EngagementActionRow)
                .where(EngagementActionRow.campaign_id == campaign_id)
                .order_by(EngagementActionRow.id.desc())
                .limit(limit)
            )
            return [self._to_action(r) for r in s.scalars(q)]
