"""Instagram comments + private replies via the Instagram API with Instagram Login (graph.instagram.com).

Docs (checked 2026-10-03):
  comments        GET  /{media_id}/comments                 https://developers.facebook.com/docs/instagram-platform/comment-moderation
  public reply    POST /{comment_id}/replies?message=...     (same page)
  private reply   POST /{ig_id}/messages {recipient:{comment_id}, message:{text}}
                                                             https://developers.facebook.com/docs/instagram-platform/private-replies
  conversations   GET  /{ig_id}/conversations?platform=instagram&user_id=<IGSID>
                                                             https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api
Live-checked 2026-10-04: comments come newest first; a comment's `from.id` works as the conversations `user_id`;
`replies{from,username}` expands inside the comments call (no extra calls). Tokens go in headers, never in URLs.
"""

import json
from datetime import datetime
from functools import cached_property
from typing import Any

import httpx

from app.domain.engagement import IgAccount, IgComment, IgMedia, IgMessage
from app.domain.errors import EngagementBlocked, EngagementError
from app.infrastructure.config import Redactor

MAX_COMMENTS_FETCHED = 2000  # hard cap per post per run
_TOKEN_CODES = {190}
_PERMISSION_CODES = {10} | set(range(200, 300))
_RATE_LIMIT_CODES = {4, 17, 32, 613}
# "Temporary issue, try again later" (https://developers.facebook.com/docs/graph-api/guides/error-handling):
# the person goes back to the queue instead of "needs you".
_TEMPORARY_CODES = {1, 2}
_STANDARD_ACCESS_SUBCODE = 2534001  # [Community] "thread does not exist" for non-testers on Standard Access
# Meta reports rate-limit usage (percent) in these response headers; stop before the next call at this level.
# https://developers.facebook.com/docs/graph-api/overview/rate-limiting (header names: [Unsure] for graph.instagram.com)
_USAGE_HEADERS = ("x-app-usage", "x-business-use-case-usage")
USAGE_STOP_PCT = 80


def usage_percent(headers: httpx.Headers) -> float:
    """Highest call_count / total_time / total_cputime percent in Meta's usage headers (0 if absent)."""
    worst = 0.0
    for name in _USAGE_HEADERS:
        raw = headers.get(name)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        entries = [data] if "call_count" in data else [e for v in data.values() if isinstance(v, list) for e in v]
        for e in entries:
            for key in ("call_count", "total_time", "total_cputime"):
                if isinstance(e.get(key), (int, float)):
                    worst = max(worst, float(e[key]))
    return worst


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


class InstagramGraphEngagement:
    def __init__(
        self,
        ig_user_id: str,
        token: str,
        graph_version: str,
        redact: Redactor,
        host: str = "https://graph.instagram.com",
        http: httpx.Client | None = None,
        max_calls: int | None = None,
    ):
        self.ig_user_id = ig_user_id
        self.token = token
        self.version = graph_version
        self.redact = redact
        self.host = host.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.usage_pct = 0.0  # last usage Meta reported
        # App-level limit: 200 calls/hour x app users (only you = 200). One adapter = one run; runs are hourly.
        self.max_calls = max_calls
        self.calls = 0

    # ------------------------------------------------------------------ helpers

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.host}/{self.version}/{path.lstrip('/')}"

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise EngagementBlocked(f"call budget for this run used ({self.max_calls} calls) — the next run continues")
        self.calls += 1
        if self.usage_pct >= USAGE_STOP_PCT:
            raise EngagementBlocked(
                f"Meta rate-limit usage is at {self.usage_pct:.0f}% — stopped before the limit; the next run continues"
            )
        try:
            r = self.http.request(method, self._url(path), headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # never reached Instagram: nothing was sent
            raise EngagementBlocked(f"Instagram API not reachable: {type(e).__name__}") from e
        except httpx.HTTPError as e:  # the request may have arrived; only the answer was lost
            raise EngagementBlocked(
                f"Instagram connection lost mid-call ({type(e).__name__}) — unknown whether it went through",
                maybe_sent=True,
            ) from e
        self.usage_pct = usage_percent(r.headers)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code < 400 and "error" not in body:
            return body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code, sub = err.get("code"), err.get("error_subcode")
        msg = self.redact(f"Instagram API error {code}/{sub}: {err.get('message') or r.text[:200]}")
        if code in _TOKEN_CODES:
            raise EngagementBlocked(f"{msg} — the Instagram token is invalid or expired (INSTAGRAM_ACCESS_TOKEN)")
        if code in _RATE_LIMIT_CODES:
            raise EngagementBlocked(f"{msg} — rate limited; the next scheduled run will continue")
        if code in _PERMISSION_CODES:
            raise EngagementBlocked(f"{msg} — missing permission or app access level")
        if sub == _STANDARD_ACCESS_SUBCODE:
            raise EngagementBlocked(
                f"{msg} — likely Standard Access: DMs only reach app testers until Meta grants Advanced Access "
                "for instagram_business_manage_messages (App Review)"
            )
        raise EngagementError(msg, retryable=code in _TEMPORARY_CODES or (code is None and r.status_code >= 500))

    def _pages(self, path: str, params: dict[str, Any], cap: int):
        url, query = path, params
        fetched = 0
        while url and fetched < cap:
            body = self._call("GET", url, params=query)
            data = body.get("data", [])
            yield from data
            fetched += len(data)
            url, query = body.get("paging", {}).get("next"), None  # `next` already carries the query

    # ------------------------------------------------------------------ port

    @cached_property
    def _me(self) -> IgAccount:
        body = self._call("GET", "me", params={"fields": "user_id,username"})
        return IgAccount(id=str(body.get("user_id") or self.ig_user_id), username=body.get("username", ""))

    def account(self) -> IgAccount:
        return self._me

    def recent_media(self, limit: int) -> list[IgMedia]:
        fields = "id,permalink,caption,timestamp,media_product_type"
        items = self._pages(f"{self.ig_user_id}/media", {"fields": fields, "limit": min(limit, 50)}, cap=limit)
        return [
            IgMedia(
                id=m["id"],
                permalink=m.get("permalink", ""),
                caption=m.get("caption", ""),
                timestamp=_ts(m["timestamp"]),
                product_type=m.get("media_product_type", ""),
            )
            for m in list(items)[:limit]
        ]

    def comments(self, media_id: str, limit: int) -> list[IgComment]:
        # Newest first (live check), but sort anyway; replies are expanded to spot comments answered by hand.
        me = self._me
        fields = "id,text,timestamp,from,username,replies{from,username}"
        rows = self._pages(f"{media_id}/comments", {"fields": fields, "limit": 50}, MAX_COMMENTS_FETCHED)
        out = []
        for c in rows:
            sender = c.get("from") or {}
            replies = (c.get("replies") or {}).get("data", [])
            owner_replied = any(
                str((r.get("from") or {}).get("id")) in {me.id, self.ig_user_id} or r.get("username") == me.username
                for r in replies
            )
            out.append(
                IgComment(
                    id=c["id"],
                    media_id=media_id,
                    text=c.get("text", ""),
                    timestamp=_ts(c["timestamp"]),
                    user_id=str(sender.get("id", "")),
                    username=sender.get("username") or c.get("username", ""),
                    owner_replied=owner_replied,
                )
            )
        out.sort(key=lambda x: x.timestamp, reverse=True)
        return out[:limit]

    def private_reply(self, comment_id: str, text: str) -> str:
        body = self._call(
            "POST",
            f"{self.ig_user_id}/messages",
            json={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )
        return str(body.get("message_id", ""))

    def reply_to_comment(self, comment_id: str, text: str) -> str:
        body = self._call("POST", f"{comment_id}/replies", data={"message": text})
        return str(body.get("id", ""))

    def messages_from_us(self, user_id: str) -> list[IgMessage] | None:
        if not user_id:
            return None
        try:
            body = self._call(
                "GET",
                f"{self.ig_user_id}/conversations",
                params={"platform": "instagram", "user_id": user_id, "fields": "messages.limit(20){message,from,created_time}"},
            )
        except EngagementError:
            return None  # can't check: the caller falls back to the ledger (+ the unverified prefix)
        me = self._me
        out = []
        for conv in body.get("data", []):
            for m in conv.get("messages", {}).get("data", []):
                sender = m.get("from") or {}
                if str(sender.get("id")) in {me.id, self.ig_user_id} or sender.get("username") == me.username:
                    out.append(IgMessage(text=m.get("message", ""), sent_at=_ts(m["created_time"])))
        return out
