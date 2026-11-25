"""Comment-to-DM campaigns: save/approve campaigns and run them (dry or live).

Safety model (see docs/COMMENT_TO_DM_PLAN.md):
- The owner approves a campaign's exact content once. Editing the content drops the approval.
- Live runs only for approved campaigns. Dry runs read Instagram but write nothing anywhere.
- At most once per comment: the ledger row is claimed *before* any Instagram call.
- Nobody is lost when a run stops: surely-not-sent → back to `queued`; outcome unknown (crash, lost answer)
  → `pending`, and the next run settles it by reading the conversation (DM there → sent, else → queued).
- One DM per user per campaign, plus an optional check of the user's conversation for a similar DM.
- Stop the run on token/permission/rate-limit errors, or after 3 failed DMs in a row. Every live run writes
  its outcome and stop reason to the audit log (`engagement_run`).
"""

import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain import engagement as rules
from app.domain.engagement import ActionState, Campaign, CampaignStatus, EngagementAction, IgAccount, IgComment, RunReport
from app.domain.errors import CampaignInvalid, EngagementBlocked, EngagementError
from app.domain.ports import AuditLog, CampaignRepository, Clock, EngagementActionRepository, InstagramEngagement

MAX_FAILED_DMS_IN_A_ROW = 3


@dataclass
class EngagementSettings:
    delay_s: tuple[float, float] = (8.0, 25.0)  # random pause between actions, looks less like a bot burst
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)


class EngagementService:
    def __init__(
        self,
        campaigns: CampaignRepository,
        actions: EngagementActionRepository,
        instagram: Callable[[], InstagramEngagement],  # lazy: only built when a call needs Instagram
        audit: AuditLog,
        clock: Clock,
        settings: EngagementSettings | None = None,
    ):
        self.campaigns = campaigns
        self.actions = actions
        self._instagram = instagram
        self.audit = audit
        self.clock = clock
        self.settings = settings or EngagementSettings()

    # ------------------------------------------------------------ campaigns

    def save(self, spec: dict[str, Any]) -> Campaign:
        """Create or update a campaign by name. Content changes drop the approval (back to draft)."""
        name = str(spec.get("name") or "").strip()
        if not name:
            raise CampaignInvalid("name is required")
        media = [str(m).strip() for m in spec.get("media") or [] if str(m).strip()]
        existing = self.campaigns.get_by_name(name)
        campaign = Campaign(
            id=existing.id if existing else secrets.token_hex(6),
            name=name,
            media_ids=[],
            keywords=[str(k).strip() for k in spec.get("keywords") or [] if str(k).strip()],
            dm_message=str(spec.get("dm_message") or "").strip(),
            replies=[str(r).strip() for r in spec.get("replies") or [] if str(r).strip()],
            fallback_reply=str(spec.get("fallback_reply") or "").strip(),
            unverified_prefix=str(spec.get("unverified_prefix") or "").strip(),
            scan_limit=int(spec.get("scan_limit", 100)),
            max_actions_per_run=int(spec.get("max_actions_per_run", 30)),
            history_check=bool(spec.get("history_check", True)),
            created_at=existing.created_at if existing else self.clock.now(),
            updated_at=self.clock.now(),
        )
        campaign.media_links = self._resolve_media(media)
        campaign.media_ids = list(campaign.media_links)
        problems = rules.validate_campaign(campaign)
        if problems:
            raise CampaignInvalid("; ".join(problems))
        if existing and existing.approved and existing.fingerprint == campaign.fingerprint:
            campaign.status, campaign.approved_fingerprint = existing.status, existing.approved_fingerprint
        elif existing and existing.status is CampaignStatus.PAUSED:
            campaign.status = CampaignStatus.PAUSED
        return self.campaigns.save(campaign)

    def _resolve_media(self, items: list[str]) -> dict[str, str]:
        """Accept media ids, post/reel links, or "latest" (the newest reel). Returns {media id: permalink}."""
        out: dict[str, str] = {}
        links = [i for i in items if not i.isdigit()]
        recent = self._instagram().recent_media(limit=50) if links else []
        for item in items:
            if item.isdigit():
                out.setdefault(item, "")
                continue
            if item.lower() == "latest":
                reels = [m for m in recent if m.product_type == "REELS"] or recent
                if not reels:
                    raise CampaignInvalid("no posts found on the Instagram account")
                out.setdefault(reels[0].id, reels[0].permalink)
                continue
            key = _link_key(item)
            match = next((m for m in recent if _link_key(m.permalink) == key), None)
            if match is None:
                raise CampaignInvalid(f"{item} is not among the 50 newest posts of the account")
            out.setdefault(match.id, match.permalink)
        return out

    def approve(self, campaign_id: str) -> Campaign:
        c = self.campaigns.get(campaign_id)
        c.status, c.approved_fingerprint, c.updated_at = CampaignStatus.ACTIVE, c.fingerprint, self.clock.now()
        self.audit.append({"event": "campaign_approved", "campaign_id": c.id, "name": c.name, "fingerprint": c.fingerprint})
        return self.campaigns.save(c)

    def pause(self, campaign_id: str) -> Campaign:
        c = self.campaigns.get(campaign_id)
        c.status, c.updated_at = CampaignStatus.PAUSED, self.clock.now()
        self.audit.append({"event": "campaign_paused", "campaign_id": c.id, "name": c.name})
        return self.campaigns.save(c)

    def list(self) -> list[Campaign]:
        return self.campaigns.list()

    def list_one(self, campaign_id: str) -> Campaign:
        return self.campaigns.get(campaign_id)

    def history(self, campaign_id: str, limit: int = 200) -> list[EngagementAction]:
        self.campaigns.get(campaign_id)
        return self.actions.list(campaign_id, limit)

    def runs(self, campaign_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Last live runs of this campaign, newest first: who started it, counts, and why it stopped."""
        c = self.campaigns.get(campaign_id)
        return self.audit.recent("engagement_run", limit, campaign_id=c.id)

    def minutes_since_manual_run(self) -> float | None:
        """Minutes since the last live run started by the owner (board or command line) ended, any campaign."""
        for run in self.audit.recent("engagement_run", 50):
            if run.get("trigger", "cli") != "scheduler" and not run.get("dry_run"):
                ended = datetime.fromisoformat(run["at"])
                return (self.clock.now() - ended).total_seconds() / 60
        return None

    def report(self, campaign_id: str) -> tuple[Campaign, list[EngagementAction]]:
        """Every comment the ledger knows for this campaign (offline)."""
        c = self.campaigns.get(campaign_id)
        return c, self.actions.list(c.id, limit=100_000)

    # ------------------------------------------------------------ runs

    def sync(self, campaign_id: str) -> RunReport:
        """Read the comments and refresh the queue in the database. Sends nothing; works for drafts too."""
        c = self.campaigns.get(campaign_id)
        report = RunReport(campaign_id=c.id, dry_run=False)
        try:
            me, comments = self._read(c, report)
        except (EngagementBlocked, EngagementError) as e:
            report.aborted = str(e)
            return report
        report.scanned = len(comments)
        report.matched = self._sync_queue(c, me, comments, self.actions.handled_comment_ids(c.id))
        return report

    def _read(self, c: Campaign, report: RunReport) -> tuple[IgAccount, list[IgComment]]:
        ig = self._instagram()
        me = ig.account()
        comments: list[IgComment] = []
        for media_id in c.media_ids:
            got = ig.comments(media_id, c.scan_limit)
            if len(got) >= c.scan_limit:  # older comments were cut off: they would never be served
                report.warnings.append(
                    f"reel {c.media_links.get(media_id) or media_id} reached scan_limit {c.scan_limit}: "
                    "older comments are not read — raise scan_limit (max 2000)"
                )
            comments += got
        return me, comments

    def _sync_queue(self, c: Campaign, me: IgAccount, comments: list[IgComment], handled: set[str]) -> int:
        """Write every not-yet-handled comment as queued (keyword) or no_keyword. Returns how many matched."""
        rows, matched = [], 0
        for comment in comments:
            if comment.user_id == me.id or comment.username.casefold() == me.username.casefold():
                continue
            hit = rules.matches_keywords(comment.text, c.keywords)
            matched += hit
            if comment.id not in handled:
                rows.append(_action(c, comment, ActionState.QUEUED if hit else ActionState.NO_KEYWORD))
        self.actions.sync(rows)
        return matched

    def run_active(self, dry_run: bool, max_actions: int | None = None, trigger: str = "cli") -> list[RunReport]:
        return [self.run(c.id, dry_run, max_actions, trigger=trigger) for c in self.campaigns.list() if c.approved]

    def run(
        self,
        campaign_id: str,
        dry_run: bool,
        max_actions: int | None = None,
        only_username: str | None = None,
        trigger: str = "cli",  # cli | board | scheduler — recorded with the run
    ) -> RunReport:
        """max_actions overrides the campaign's per-run limit (manual "next batch of N"), never above it.
        only_username limits the run to one commenter (live test on a tester account).
        Every live run, however it ends (even a crash), leaves one `engagement_run` line in the audit log
        with its counts and the reason it stopped."""
        report = RunReport(campaign_id=campaign_id, dry_run=dry_run)
        ig: InstagramEngagement | None = None
        try:
            c = self.campaigns.get(campaign_id)
            report.campaign_id = c.id
            if not dry_run and not c.approved:
                report.aborted = f"campaign '{c.name}' is {c.status.value}, not approved — approve it first (dry runs are allowed)"
                return report
            ig = self._instagram()
            self._run(c, ig, report, max_actions, only_username)
            return report
        except Exception as e:
            report.aborted = report.aborted or f"crashed: {type(e).__name__}: {e}"
            raise
        finally:
            report.api_calls = getattr(ig, "calls", None)
            report.usage_pct = getattr(ig, "usage_pct", None)
            if not dry_run:
                summary = {k: v for k, v in report.to_dict().items() if k != "items"}
                self.audit.append({"event": "engagement_run", "trigger": trigger, **summary})

    def _run(self, c: Campaign, ig: InstagramEngagement, report: RunReport, max_actions: int | None, only_username: str | None) -> None:
        dry_run = report.dry_run
        limit = min(max_actions, c.max_actions_per_run) if max_actions else c.max_actions_per_run
        now = self.clock.now()
        try:
            me, comments = self._read(c, report)
            if not dry_run:
                self._recover_pending(c, ig, report)
        except (EngagementBlocked, EngagementError) as e:
            report.aborted = str(e)
            return

        report.scanned = len(comments)
        handled = self.actions.handled_comment_ids(c.id)
        if not dry_run:
            self._sync_queue(c, me, comments, handled)  # the database always shows every comment's state
        dm_users = self.actions.users_with_dm(c.id)
        used_replies = list(reversed(self.actions.recent_replies(c.id, 10)))
        live_actions, failed_in_a_row = 0, 0

        # Oldest first: comments closest to the 7-day limit are served first.
        for comment in sorted(comments, key=lambda x: x.timestamp):
            if comment.user_id == me.id or comment.username.casefold() == me.username.casefold():
                continue
            if only_username and comment.username.casefold() != only_username.lstrip("@").casefold():
                continue
            if not rules.matches_keywords(comment.text, c.keywords):
                continue
            report.matched += 1
            if comment.id in handled:
                report.already_handled += 1
                continue

            action = _action(c, comment)
            if not rules.within_private_reply_window(comment.timestamp, now):
                action.state = ActionState.SKIPPED_EXPIRED
            elif comment.user_id in dm_users:
                action.state = ActionState.SKIPPED_SAME_USER
            if action.state is not ActionState.PENDING:
                self._record_skip(report, action, dry_run)
                handled.add(comment.id)
                continue

            if live_actions >= limit:
                report.deferred += 1
                continue

            unverified = False
            if c.history_check:
                try:
                    ours = ig.messages_from_us(comment.user_id)
                except EngagementBlocked as e:
                    report.aborted = str(e)
                    break
                except EngagementError:
                    ours = None
                if ours is None:
                    unverified = True
                    action.error = "couldn't read the DM conversation; sent with the unverified prefix"
                elif rules.already_contacted(c.dm_message, ours):
                    action.state = ActionState.SKIPPED_HISTORY
                    self._record_skip(report, action, dry_run)
                    dm_users.add(comment.user_id)
                    handled.add(comment.id)
                    continue

            dm_text = rules.render(c.dm_message, comment)
            if unverified and c.unverified_prefix:
                dm_text = rules.render(c.unverified_prefix, comment) + "\n" + dm_text
            public_reply = not comment.owner_replied  # you already answered publicly: DM only

            dm_users.add(comment.user_id)
            handled.add(comment.id)
            if dry_run:
                reply = None
                if public_reply:
                    reply = rules.render(rules.pick_reply(c.replies, used_replies, self.settings.rng), comment)
                    used_replies.append(reply)
                report.items.append({**action.to_dict(), "state": "would_send", "dm": dm_text, "reply": reply})
                live_actions += 1
                continue

            if live_actions:
                self.settings.sleep(self.settings.rng.uniform(*self.settings.delay_s))
            if not self.actions.claim(action):
                report.already_handled += 1
                continue
            live_actions += 1
            try:
                ok = self._execute(c, ig, comment, action, dm_text, public_reply, used_replies)
            except EngagementBlocked as e:
                self._stopped_mid_action(c, action, e)
                report.items.append(action.to_dict())
                report.aborted = str(e)
                break
            report.items.append(action.to_dict())
            if action.state is ActionState.QUEUED:  # temporary Instagram error: not sent, try again next run
                dm_users.discard(comment.user_id)
                handled.discard(comment.id)
            failed_in_a_row = 0 if ok else failed_in_a_row + 1
            if failed_in_a_row >= MAX_FAILED_DMS_IN_A_ROW:
                report.aborted = f"{failed_in_a_row} DMs failed in a row — stopped to protect the account; check the errors"
                break

    def _stopped_mid_action(self, c: Campaign, action: EngagementAction, e: EngagementBlocked) -> None:
        """The run must stop while handling this person. Nobody may be lost or messaged twice."""
        if action.dm_message_id:  # the DM went out; only the public reply was stopped
            action.state = ActionState.DM_ONLY
            action.error = "; ".join(filter(None, [action.error, f"public reply not posted: {e}"]))
        elif e.maybe_sent:  # the answer was lost: the next run reads the conversation to settle it
            action.state = ActionState.PENDING
            action.error = f"{e} — the next run checks the conversation"
        else:  # surely not sent: back in the queue for the next run
            action.state = ActionState.QUEUED
            action.error = f"not sent, back in the queue: {e}"
        action.updated_at = self.clock.now()
        self.actions.update(action)
        self.audit.append({"event": "engagement_action", "campaign_id": c.id, **action.to_dict()})

    def _recover_pending(self, c: Campaign, ig: InstagramEngagement, report: RunReport) -> None:
        """`pending` = a run was interrupted after claiming the row (crash, PC off, lost connection), so nobody
        knows if the DM went out. The run lock guarantees no other run is using it now. Read the conversation:
        this campaign's DM is there → sent; readable and not there → back in the queue (Meta also refuses a
        second private reply to the same comment); unreadable → stays `pending` for the owner ("needs you")."""
        for action in self.actions.list(c.id, limit=100_000):
            if action.state is not ActionState.PENDING:
                continue
            try:
                ours = ig.messages_from_us(action.user_id)
            except EngagementError:
                continue
            if ours is None:
                continue
            if rules.already_contacted(c.dm_message, ours):
                action.state = ActionState.DM_ONLY
                action.error = "interrupted run; the DM was found in the conversation (public reply may be missing)"
            else:
                action.state = ActionState.QUEUED
                action.error = "interrupted run; no DM in the conversation, so it's back in the queue"
            action.updated_at = self.clock.now()
            self.actions.update(action)
            self.audit.append({"event": "engagement_recovered", "campaign_id": c.id, **action.to_dict()})
            report.recovered += 1

    def _execute(
        self,
        c: Campaign,
        ig: InstagramEngagement,
        comment: IgComment,
        action: EngagementAction,
        dm_text: str,
        public_reply: bool,
        used: list[str],
    ) -> bool:
        """DM first (time-limited, the point of the campaign), then the public reply. Returns DM success."""
        dm_ok = False
        try:
            action.dm_message_id = ig.private_reply(comment.id, dm_text)
            dm_ok = True
        except EngagementError as e:
            action.error = "; ".join(filter(None, [action.error, str(e)]))
            if e.retryable:  # Meta's "temporary, try later": no fallback reply, back in the queue
                action.state, action.error = ActionState.QUEUED, f"temporary Instagram error, back in the queue: {e}"
                action.updated_at = self.clock.now()
                self.actions.update(action)
                self.audit.append({"event": "engagement_action", "campaign_id": c.id, **action.to_dict()})
                return False

        reply = None
        if public_reply:
            reply = rules.render(rules.pick_reply(c.replies, used, self.settings.rng), comment) if dm_ok else (
                rules.render(c.fallback_reply, comment) if c.fallback_reply else None
            )
        if reply:
            try:
                action.reply_comment_id = ig.reply_to_comment(comment.id, reply)
                action.reply_text = reply
                used.append(reply)
            except EngagementError as e:
                action.error = "; ".join(filter(None, [action.error, f"public reply: {e}"]))

        if dm_ok:
            action.state = ActionState.DONE if action.reply_comment_id else ActionState.DM_ONLY
        else:
            action.state = ActionState.DM_FAILED
        action.updated_at = self.clock.now()
        self.actions.update(action)
        self.audit.append({"event": "engagement_action", "campaign_id": c.id, **action.to_dict()})
        return dm_ok

    def status(self, campaign_id: str) -> dict[str, Any]:
        """Offline: what the ledger knows. No Instagram calls."""
        c = self.campaigns.get(campaign_id)
        return {**c.to_dict(), "ledger": self.actions.counts(c.id)}

    def forget(self, username: str) -> int:
        n = self.actions.forget_user(username.lstrip("@"))
        self.audit.append({"event": "engagement_forget", "rows_deleted": n})  # no username: that's the point
        return n

    def _record_skip(self, report: RunReport, action: EngagementAction, dry_run: bool) -> None:
        if not dry_run:
            self.actions.claim(action)
        report.items.append(action.to_dict())


def _action(c: Campaign, comment: IgComment, state: ActionState = ActionState.PENDING) -> EngagementAction:
    return EngagementAction(
        campaign_id=c.id,
        comment_id=comment.id,
        media_id=comment.media_id,
        user_id=comment.user_id,
        username=comment.username,
        comment_text=comment.text,
        comment_at=comment.timestamp,
        state=state,
    )


def _link_key(url: str) -> str:
    """https://www.instagram.com/reel/ABC123/?igsh=x -> ABC123 (the shortcode; p/ and reel/ links both work)."""
    path = url.split("?")[0].split("#")[0].rstrip("/")
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else path
