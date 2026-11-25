"""Comment-to-DM engagement: entities and pure rules. No I/O.

A campaign watches one or more Instagram posts. A comment that matches a keyword gets one private reply (DM)
and one public reply picked from a rotation. Every handled comment is recorded once (the ledger), so a run
can be repeated any number of times without messaging anyone twice.
"""

import difflib
import hashlib
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

# Meta: a private reply must be sent within 7 days of the comment, and only once per comment.
# https://developers.facebook.com/docs/instagram-platform/private-replies (checked 2026-10-03)
PRIVATE_REPLY_WINDOW = timedelta(days=7)
WINDOW_SAFETY_MARGIN = timedelta(hours=1)
SIMILARITY_THRESHOLD = 0.85
# Send API: "Message text must be UTF-8 and be a 1000 bytes or less."
# https://developers.facebook.com/docs/messenger-platform/instagram/features/send-message (checked 2026-10-03)
DM_MAX_BYTES = 1000


class CampaignStatus(StrEnum):
    DRAFT = "draft"  # created or edited, not approved: dry runs only
    ACTIVE = "active"  # approved by the owner: live runs allowed
    PAUSED = "paused"


class ActionState(StrEnum):
    QUEUED = "queued"  # matched, waiting for a batch (written by every live run / sync)
    NO_KEYWORD = "no_keyword"  # comment without a keyword: shown in the report, never messaged
    PENDING = "pending"  # claimed, outcome unknown (run interrupted); the next run checks the conversation
    DONE = "done"  # DM sent and public reply posted
    DM_ONLY = "dm_only"  # DM sent, public reply failed or not configured
    DM_FAILED = "dm_failed"  # DM refused (privacy, expired, ...); fallback reply may have been posted
    SKIPPED_SAME_USER = "skipped_same_user"  # this user already got the DM in this campaign
    SKIPPED_HISTORY = "skipped_history"  # our conversation already has this campaign's DM/link (e.g. sent by hand)
    SKIPPED_EXPIRED = "skipped_expired"  # older than the 7-day private-reply window


@dataclass
class Campaign:
    id: str
    name: str
    media_ids: list[str]
    keywords: list[str]
    dm_message: str
    replies: list[str]
    fallback_reply: str = ""  # public reply when the DM fails; empty = no public reply then
    # Prepended to the DM when we can't read the person's conversation (so we can't rule out a duplicate).
    unverified_prefix: str = ""
    scan_limit: int = 100  # newest N comments per post, per run
    max_actions_per_run: int = 30
    history_check: bool = True
    status: CampaignStatus = CampaignStatus.DRAFT
    approved_fingerprint: str | None = None
    media_links: dict[str, str] = field(default_factory=dict)  # media id -> permalink (for reports)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """Hash of everything that changes what gets sent. Approval is tied to it."""
        content = {
            "media_ids": sorted(self.media_ids),
            "keywords": sorted(self.keywords),
            "dm_message": self.dm_message,
            "replies": self.replies,
            "fallback_reply": self.fallback_reply,
            "unverified_prefix": self.unverified_prefix,
        }
        return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

    @property
    def approved(self) -> bool:
        return self.status is CampaignStatus.ACTIVE and self.approved_fingerprint == self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "approved": self.approved,
            "media_ids": self.media_ids,
            "media_links": self.media_links,
            "keywords": self.keywords,
            "dm_message": self.dm_message,
            "replies": self.replies,
            "fallback_reply": self.fallback_reply,
            "unverified_prefix": self.unverified_prefix,
            "scan_limit": self.scan_limit,
            "max_actions_per_run": self.max_actions_per_run,
            "history_check": self.history_check,
        }


@dataclass(frozen=True)
class IgAccount:
    id: str
    username: str


@dataclass(frozen=True)
class IgMedia:
    id: str
    permalink: str
    caption: str
    timestamp: datetime
    product_type: str  # REELS, FEED, ...


@dataclass(frozen=True)
class IgComment:
    id: str
    media_id: str
    text: str
    timestamp: datetime
    user_id: str
    username: str
    owner_replied: bool = False  # the account owner already replied publicly under this comment


@dataclass(frozen=True)
class IgMessage:
    """A message the account owner sent in a DM conversation."""

    text: str
    sent_at: datetime


@dataclass
class EngagementAction:
    campaign_id: str
    comment_id: str
    media_id: str
    user_id: str
    username: str
    comment_text: str
    comment_at: datetime
    state: ActionState
    dm_message_id: str | None = None
    reply_text: str | None = None
    reply_comment_id: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "media_id": self.media_id,
            "username": self.username,
            "comment": self.comment_text,
            "comment_at": self.comment_at.isoformat(),
            "state": self.state.value,
            "dm_message_id": self.dm_message_id,
            "reply": self.reply_text,
            "reply_comment_id": self.reply_comment_id,
            "error": self.error,
        }


@dataclass
class RunReport:
    campaign_id: str
    dry_run: bool
    scanned: int = 0
    matched: int = 0
    already_handled: int = 0
    deferred: int = 0  # matched but over max_actions_per_run; picked up next run
    recovered: int = 0  # `pending` rows from an interrupted run, settled by reading the conversation
    aborted: str | None = None  # why the run stopped early (the root cause, kept in logs/engagement.jsonl)
    api_calls: int | None = None
    usage_pct: float | None = None  # Meta's rate-limit usage after the last call
    warnings: list[str] = field(default_factory=list)  # ran fine, but the owner should know (e.g. comments cut off)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.aborted is None

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        return {
            "ok": self.ok,
            "campaign_id": self.campaign_id,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "matched": self.matched,
            "already_handled": self.already_handled,
            "deferred": self.deferred,
            "recovered": self.recovered,
            "counts": counts,
            "aborted": self.aborted,
            "api_calls": self.api_calls,
            "usage_pct": self.usage_pct,
            "warnings": self.warnings,
            "items": self.items,
        }


# ---------------------------------------------------------------- rules

_ARABIC_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # harakat + tatweel
_ARABIC_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_REPEATS = re.compile(r"(.)\1+")
_URL = re.compile(r"https?://\S+")


def normalize(text: str) -> str:
    """Case-, accent- and spelling-tolerant form for comparing comments and messages (Arabic + English)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _ARABIC_MARKS.sub("", text).translate(_ARABIC_LETTERS)
    text = _NON_WORD.sub(" ", text).replace("_", " ")
    return _SPACES.sub(" ", text).strip()


def _squash(text: str) -> str:
    """normalize + collapse repeated letters, so "skilllll" and "سكييييل" still match."""
    return _REPEATS.sub(r"\1", normalize(text))


def matches_keywords(comment: str, keywords: list[str]) -> bool:
    body = _squash(comment)
    return any(k and k in body for k in (_squash(k) for k in keywords))


_DRIVE_ID = re.compile(r"(?:/folders/|/d/|[?&]id=)([A-Za-z0-9_-]{10,})")


def link_keys(text: str) -> set[str]:
    """Comparable keys for the links in a text. Google Drive links reduce to the file/folder id, so
    ".../folders/ID?usp=sharing" and ".../drive/u/0/folders/ID" are the same; other links drop query and slash."""
    keys = set()
    for url in _URL.findall(text):
        url = url.rstrip(").,!؟?»\"'")
        drive = _DRIVE_ID.search(url) if "drive.google." in url or "docs.google." in url else None
        if drive:
            keys.add(f"gdrive:{drive.group(1)}")
            continue
        host_path = url.split("://", 1)[1].split("?")[0].split("#")[0].rstrip("/")
        host, _, path = host_path.partition("/")
        keys.add(f"{host.lower().removeprefix('www.')}/{path}")
    return keys


def similar(sent: str, candidate: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """True when `candidate` (a message already in the conversation) is our DM.
    If our DM has a link, only the link decides: another campaign's DM with the same wording but a different
    link is NOT a match. Text similarity is used only for DMs without links."""
    ours = link_keys(sent)
    if ours:
        return bool(ours & link_keys(candidate))
    a, b = normalize(sent), normalize(candidate)
    if not a or not b:
        return False
    if a in b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


# Ledger states that are not final: the comment is still open for a batch / re-check.
OPEN_STATES = frozenset({ActionState.QUEUED, ActionState.NO_KEYWORD})


def already_contacted(dm_message: str, ours: list[IgMessage]) -> bool:
    """This campaign's DM is already in the conversation: same link, or (almost) the same text.
    Campaign-specific on purpose: an unrelated chat with the person doesn't count."""
    return any(similar(dm_message, m.text) for m in ours)


def render(template: str, comment: IgComment) -> str:
    return template.replace("{username}", comment.username)


def pick_reply(replies: list[str], recently_used: list[str], rng: random.Random) -> str:
    """Random reply that is not one of the last few used, so neighbouring comments get different replies."""
    if not replies:
        raise ValueError("no replies configured")
    avoid = set(recently_used[-min(3, len(replies) - 1) :]) if len(replies) > 1 else set()
    choices = [r for r in replies if r not in avoid] or replies
    return rng.choice(choices)


def within_private_reply_window(comment_at: datetime, now: datetime) -> bool:
    return now - comment_at < PRIVATE_REPLY_WINDOW - WINDOW_SAFETY_MARGIN


def validate_campaign(c: Campaign) -> list[str]:
    """Problems that make a campaign unusable. Empty list = fine."""
    problems = []
    if not c.media_ids:
        problems.append("media: at least one Instagram post/reel is required")
    if not [k for k in c.keywords if normalize(k)]:
        problems.append("keywords: at least one keyword is required")
    if not c.dm_message.strip():
        problems.append("dm_message is empty")
    elif len((c.unverified_prefix + "\n" + c.dm_message).replace("{username}", "x" * 30).encode("utf-8")) > DM_MAX_BYTES:
        problems.append(f"dm_message is over {DM_MAX_BYTES} bytes (Arabic letters are 2 bytes, emoji 4)")
    if not [r for r in c.replies if r.strip()]:
        problems.append("replies: at least one public reply is required")
    if len(set(c.replies)) != len(c.replies):
        problems.append("replies: duplicates in the list")
    for r in c.replies + ([c.fallback_reply] if c.fallback_reply else []):
        if len(r) > 2200:
            problems.append("a reply is longer than 2200 characters")
    if not 1 <= c.scan_limit <= 2000:
        problems.append("scan_limit must be between 1 and 2000")
    if not 1 <= c.max_actions_per_run <= 200:
        problems.append("max_actions_per_run must be between 1 and 200")
    return problems
