"""Ports: interfaces the application layer depends on. Infrastructure provides the implementations."""

from datetime import datetime
from typing import Any, Protocol

from app.domain.engagement import Campaign, EngagementAction, IgAccount, IgComment, IgMedia, IgMessage
class AuditLog(Protocol):
    def append(self, event: dict[str, Any]) -> None: ...

    def recent(self, event: str, limit: int, **match: Any) -> list[dict[str, Any]]:
        """Newest first: the last `limit` events of this kind whose fields equal `match`."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...


# ---------------------------------------------------------------- comment-to-DM engagement


class InstagramEngagement(Protocol):
    """Instagram comments + private replies. Errors: EngagementError (one comment) / EngagementBlocked (stop run)."""

    def account(self) -> IgAccount: ...

    def recent_media(self, limit: int) -> list[IgMedia]: ...

    def comments(self, media_id: str, limit: int) -> list[IgComment]:
        """Newest-first top-level comments, at most `limit`."""
        ...

    def private_reply(self, comment_id: str, text: str) -> str:
        """DM the commenter (once per comment, within 7 days). Returns the message id."""
        ...

    def reply_to_comment(self, comment_id: str, text: str) -> str:
        """Public reply under the comment. Returns the new comment id."""
        ...

    def messages_from_us(self, user_id: str) -> list[IgMessage] | None:
        """Our messages in the conversation with this user (newest 20 messages only); [] = none, None = could not check."""
        ...


class CampaignRepository(Protocol):
    def save(self, campaign: Campaign) -> Campaign: ...

    def get(self, campaign_id: str) -> Campaign: ...

    def get_by_name(self, name: str) -> Campaign | None: ...

    def list(self) -> list[Campaign]: ...


class EngagementActionRepository(Protocol):
    def claim(self, action: EngagementAction) -> bool:
        """Take the comment for this run: insert, or move an open (queued/no_keyword) row to the action's state.
        False if the row is already final (another run has it)."""
        ...

    def sync(self, actions: list[EngagementAction]) -> None:
        """Upsert open rows (queued / no_keyword). Never touches final rows."""
        ...

    def update(self, action: EngagementAction) -> None: ...

    def handled_comment_ids(self, campaign_id: str) -> set[str]:
        """Comments with a final state (open rows are not handled)."""
        ...

    def users_with_dm(self, campaign_id: str) -> set[str]: ...

    def counts(self, campaign_id: str) -> dict[str, int]: ...

    def forget_user(self, username: str) -> int:
        """Delete every record of this username (privacy request). Returns rows deleted."""
        ...

    def recent_replies(self, campaign_id: str, limit: int) -> list[str]: ...

    def list(self, campaign_id: str, limit: int = 200) -> list[EngagementAction]: ...


class TaskScheduler(Protocol):
    """OS-level job that runs the engagement batch on an interval."""

    def install(self, every_minutes: int) -> dict[str, Any]: ...

    def remove(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...
