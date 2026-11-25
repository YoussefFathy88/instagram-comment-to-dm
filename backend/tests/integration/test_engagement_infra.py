"""Comment-to-DM infrastructure: SQLite repositories (real migrations), Instagram adapter against mocked HTTP."""

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.domain.engagement import ActionState, Campaign, CampaignStatus, EngagementAction
from app.domain.errors import CampaignNotFound, EngagementBlocked, EngagementError
from app.infrastructure.config import Redactor
from app.infrastructure.db.engine import make_session_factory
from app.infrastructure.db.repositories import SqlCampaignRepository, SqlEngagementActionRepository
from app.infrastructure.files.run_lock import RunLock, RunLocked
from app.infrastructure.instagram.engagement_graph import InstagramGraphEngagement

TOKEN = "IGAAtesttoken" + "x" * 60
BASE = "https://graph.instagram.com/v26.0"
AT = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def sessions(tmp_path):
    return make_session_factory(tmp_path / "db" / "test.db")


def _campaign() -> Campaign:
    return Campaign(id="c1", name="skill-dm", media_ids=["m1"], keywords=["skill"], dm_message="hi", replies=["a", "b"])


def _action(comment_id="k1", user="u1", state=ActionState.PENDING) -> EngagementAction:
    return EngagementAction("c1", comment_id, "m1", user, "name", "skill pls", AT, state)


# ---------------------------------------------------------------- repositories


def test_campaign_roundtrip_by_id_and_name(sessions):
    repo = SqlCampaignRepository(sessions)
    c = _campaign()
    c.status, c.approved_fingerprint = CampaignStatus.ACTIVE, c.fingerprint
    repo.save(c)
    got = repo.get("skill-dm")
    assert got.id == "c1" and got.approved and got.replies == ["a", "b"] and got.created_at.tzinfo
    assert repo.get_by_name("nope") is None
    with pytest.raises(CampaignNotFound):
        repo.get("missing")


def test_action_claim_is_once_per_comment(sessions):
    repo = SqlEngagementActionRepository(sessions)
    assert repo.claim(_action())
    assert not repo.claim(_action())
    assert repo.handled_comment_ids("c1") == {"k1"}


def test_counts_and_forget_user(sessions):
    repo = SqlEngagementActionRepository(sessions)
    repo.claim(_action("k1", "u1"))
    repo.claim(_action("k2", "u2", ActionState.SKIPPED_EXPIRED))
    assert repo.counts("c1") == {"pending": 1, "skipped_expired": 1}
    assert repo.forget_user("NAME") == 2 and repo.counts("c1") == {}


def test_action_update_and_queries(sessions):
    repo = SqlEngagementActionRepository(sessions)
    a = _action()
    repo.claim(a)
    a.state, a.dm_message_id, a.reply_text, a.reply_comment_id = ActionState.DONE, "msg", "a", "r1"
    repo.update(a)
    b = _action("k2", "u2", ActionState.SKIPPED_EXPIRED)
    repo.claim(b)
    c = _action("k3", "u3")
    repo.claim(c)
    c.state, c.reply_text = ActionState.DM_ONLY, "b"
    repo.update(c)
    assert repo.users_with_dm("c1") == {"u1", "u3"}
    assert repo.recent_replies("c1", 5) == ["b", "a"]
    rows = repo.list("c1")
    assert [r.comment_id for r in rows] == ["k3", "k2", "k1"] and rows[2].comment_at == AT


def test_run_lock(tmp_path):
    lock = RunLock(tmp_path / "engagement.lock")
    with lock:
        with pytest.raises(RunLocked):
            with RunLock(tmp_path / "engagement.lock"):
                pass
    with RunLock(tmp_path / "engagement.lock"):
        pass


# ---------------------------------------------------------------- Instagram adapter


def adapter() -> InstagramGraphEngagement:
    return InstagramGraphEngagement("17841400000", TOKEN, "v26.0", Redactor([TOKEN]), http=httpx.Client())


@respx.mock
def test_comments_paginate_sort_newest_and_send_token_in_header():
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"user_id": "17841400000", "username": "owner"}))
    first = respx.get(f"{BASE}/m1/comments").mock(side_effect=[
        httpx.Response(200, json={
            "data": [{"id": "1", "text": "old skill", "timestamp": "2026-10-01T10:00:00+0000", "from": {"id": "u1", "username": "a"},
                      "replies": {"data": [{"from": {"id": "x"}, "username": "someone"}, {"from": {"id": "17841400000"}, "username": "owner"}]}}],
            "paging": {"next": "https://graph.instagram.com/v26.0/m1/comments?after=X"},
        }),
        httpx.Response(200, json={
            "data": [{"id": "2", "text": "new", "timestamp": "2026-10-02T10:00:00+0000", "from": {"id": "u2", "username": "b"}}]
        }),
    ])
    out = adapter().comments("m1", limit=5)
    assert [c.id for c in out] == ["2", "1"] and out[1].username == "a"
    assert out[1].owner_replied and not out[0].owner_replied
    req = first.calls[0].request
    assert first.calls[1].request.url.params["after"] == "X"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}" and TOKEN not in str(req.url)


@respx.mock
def test_private_reply_body_and_public_reply():
    dm = respx.post(f"{BASE}/17841400000/messages").mock(return_value=httpx.Response(200, json={"recipient_id": "u", "message_id": "mid"}))
    rp = respx.post(f"{BASE}/k1/replies").mock(return_value=httpx.Response(200, json={"id": "r1"}))
    ig = adapter()
    assert ig.private_reply("k1", "أهلاً") == "mid"
    assert json.loads(dm.calls[0].request.content) == {"recipient": {"comment_id": "k1"}, "message": {"text": "أهلاً"}}
    assert ig.reply_to_comment("k1", "done") == "r1"
    assert b"message=done" in rp.calls[0].request.content


@pytest.mark.parametrize(
    "error,exc,text",
    [
        ({"code": 190, "message": f"bad token {TOKEN}"}, EngagementBlocked, "expired"),
        ({"code": 4, "message": "limit"}, EngagementBlocked, "rate limited"),
        ({"code": 10, "message": "perm"}, EngagementBlocked, "permission"),
        ({"code": 100, "error_subcode": 2534001, "message": "thread"}, EngagementBlocked, "Advanced Access"),
        ({"code": 100, "error_subcode": 2534014, "message": "user not found"}, EngagementError, "user not found"),
    ],
)
@respx.mock
def test_error_classification_and_redaction(error, exc, text):
    respx.post(f"{BASE}/17841400000/messages").mock(return_value=httpx.Response(400, json={"error": error}))
    with pytest.raises(exc) as e:
        adapter().private_reply("k1", "x")
    assert text in str(e.value) and TOKEN not in str(e.value)


@pytest.mark.parametrize("status,body,retryable", [
    (500, {"error": {"code": 2, "message": "Service temporarily unavailable"}}, True),
    (500, {"error": {"code": 1, "message": "unknown"}}, True),
    (502, None, True),
    (400, {"error": {"code": 100, "message": "user not found"}}, False),
])
@respx.mock
def test_temporary_meta_errors_are_retryable(status, body, retryable):
    resp = httpx.Response(status, json=body) if body else httpx.Response(status, text="bad gateway")
    respx.post(f"{BASE}/17841400000/messages").mock(return_value=resp)
    with pytest.raises(EngagementError) as e:
        adapter().private_reply("k1", "x")
    assert e.value.retryable is retryable


@pytest.mark.parametrize("exc,maybe_sent", [
    (httpx.ConnectError("no network"), False),  # never reached Instagram
    (httpx.ConnectTimeout("slow dns"), False),
    (httpx.ReadTimeout("no answer"), True),  # may have arrived; the next run checks the chat
    (httpx.RemoteProtocolError("dropped"), True),
])
@respx.mock
def test_network_errors_say_whether_the_message_may_have_gone_out(exc, maybe_sent):
    respx.post(f"{BASE}/17841400000/messages").mock(side_effect=exc)
    with pytest.raises(EngagementBlocked) as e:
        adapter().private_reply("k1", "x")
    assert e.value.maybe_sent is maybe_sent


@respx.mock
def test_audit_log_recent_returns_newest_matching_runs(tmp_path):
    from app.infrastructure.audit_log import JsonlAuditLog

    log = JsonlAuditLog(tmp_path / "engagement.jsonl", Redactor([TOKEN]))
    log.append({"event": "post_scheduled", "file": "a.mp4"})
    log.append({"event": "engagement_run", "campaign_id": "c1", "aborted": None})
    log.append({"event": "engagement_run", "campaign_id": "c2", "aborted": None})
    log.append({"event": "engagement_run", "campaign_id": "c1", "aborted": f"token {TOKEN} expired"})
    runs = log.recent("engagement_run", 5, campaign_id="c1")
    assert len(runs) == 2 and TOKEN not in runs[0]["aborted"] and runs[1]["aborted"] is None


@respx.mock
def test_messages_from_us_keeps_only_ours_with_time_and_degrades_to_none():
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"user_id": "17841400000", "username": "owner"}))
    route = respx.get(f"{BASE}/17841400000/conversations").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "conv", "messages": {"data": [
            {"message": "thanks!", "from": {"id": "u1", "username": "fan"}, "created_time": "2026-10-02T22:20:00+0000"},
            {"message": "here is the skill", "from": {"id": "17841400000", "username": "owner"}, "created_time": "2026-10-02T22:16:14+0000"},
        ]}}]})
    )
    ig = adapter()
    msgs = ig.messages_from_us("u1")
    assert [m.text for m in msgs] == ["here is the skill"]
    assert msgs[0].sent_at == datetime(2026, 10, 2, 22, 16, 14, tzinfo=timezone.utc)
    assert route.calls[0].request.url.params["user_id"] == "u1"
    route.mock(return_value=httpx.Response(400, json={"error": {"code": 100, "message": "bad user_id"}}))
    assert ig.messages_from_us("u1") is None


def test_redactor_hides_instagram_tokens_but_not_message_ids():
    r = Redactor([])
    assert "IGAA" not in r("token IGAA" + "a" * 50)
    assert r("aWdfZAG1faXRlbToxOklHTWVzc2FnZA") == "aWdfZAG1faXRlbToxOklHTWVzc2FnZA"


@respx.mock
def test_setup_instagram_reads_account_and_writes_user_id(tmp_path):
    from pydantic import SecretStr

    from app.infrastructure.config import Settings
    from app.infrastructure.setup_tools import setup_instagram

    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"user_id": "1784", "username": "owner", "account_type": "MEDIA_CREATOR"}))
    env = tmp_path / ".env"
    env.write_text("INSTAGRAM_USER_ID=\nOTHER=1\n", encoding="utf-8")
    settings = Settings(_env_file=None, instagram_access_token=SecretStr(TOKEN))
    out = setup_instagram(settings, Redactor([TOKEN]), env, write=True)
    assert out == {"username": "owner", "account_type": "MEDIA_CREATOR", "user_id": "1784", "written": True}
    assert "INSTAGRAM_USER_ID=1784" in env.read_text(encoding="utf-8") and "OTHER=1" in env.read_text(encoding="utf-8")


@respx.mock
def test_stops_before_next_call_when_rate_limit_usage_is_high():
    usage = json.dumps({"17841400000": [{"type": "instagram", "call_count": 85, "total_cputime": 10, "total_time": 12}]})
    respx.post(f"{BASE}/k1/replies").mock(return_value=httpx.Response(200, json={"id": "r1"}, headers={"X-Business-Use-Case-Usage": usage}))
    ig = adapter()
    assert ig.reply_to_comment("k1", "ok") == "r1"  # the call that reported 85% still returns its result
    assert ig.usage_pct == 85
    with pytest.raises(EngagementBlocked, match="85%"):
        ig.reply_to_comment("k1", "again")


def test_usage_percent_reads_app_usage_header():
    from app.infrastructure.instagram.engagement_graph import usage_percent

    assert usage_percent(httpx.Headers({"X-App-Usage": '{"call_count": 12, "total_time": 40, "total_cputime": 5}'})) == 40
    assert usage_percent(httpx.Headers({})) == 0


@respx.mock
def test_call_budget_per_run():
    respx.post(f"{BASE}/k1/replies").mock(return_value=httpx.Response(200, json={"id": "r1"}))
    ig = InstagramGraphEngagement("17841400000", TOKEN, "v26.0", Redactor([TOKEN]), http=httpx.Client(), max_calls=2)
    ig.reply_to_comment("k1", "a")
    ig.reply_to_comment("k1", "b")
    with pytest.raises(EngagementBlocked, match="call budget"):
        ig.reply_to_comment("k1", "c")


@respx.mock
def test_token_refresh_weekly_writes_env_and_state(tmp_path):
    from pydantic import SecretStr

    from app.infrastructure.config import Settings
    from app.infrastructure.instagram.token_refresh import refresh_instagram_token

    new = "IGAAnewtoken" + "y" * 60
    route = respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": new, "token_type": "bearer", "expires_in": 5184000})
    )
    env, state = tmp_path / ".env", tmp_path / "data" / "instagram_token.json"
    env.write_text(f"INSTAGRAM_ACCESS_TOKEN={TOKEN}\n", encoding="utf-8")
    settings = Settings(_env_file=None, instagram_access_token=SecretStr(TOKEN))
    out = refresh_instagram_token(settings, Redactor([TOKEN]), env, state)
    assert out["refreshed"] and new in env.read_text(encoding="utf-8") and TOKEN not in env.read_text(encoding="utf-8")
    assert route.calls[0].request.url.params["grant_type"] == "ig_refresh_token"
    again = refresh_instagram_token(settings, Redactor([TOKEN]), env, state)
    assert not again["refreshed"] and "not due" in again["reason"] and len(route.calls) == 1


@respx.mock
def test_token_refresh_failure_is_reported_and_redacted(tmp_path):
    from pydantic import SecretStr

    from app.infrastructure.config import Settings
    from app.infrastructure.instagram.token_refresh import refresh_instagram_token

    respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"code": 190, "message": f"bad {TOKEN}"}})
    )
    env = tmp_path / ".env"
    env.write_text(f"INSTAGRAM_ACCESS_TOKEN={TOKEN}\n", encoding="utf-8")
    settings = Settings(_env_file=None, instagram_access_token=SecretStr(TOKEN))
    out = refresh_instagram_token(settings, Redactor([TOKEN]), env, tmp_path / "s.json")
    assert not out["refreshed"] and TOKEN not in out["reason"] and TOKEN in env.read_text(encoding="utf-8")


def test_sync_upserts_open_rows_and_claim_takes_them(sessions):
    repo = SqlEngagementActionRepository(sessions)
    repo.sync([_action("k1", "u1", ActionState.QUEUED), _action("k2", "u2", ActionState.NO_KEYWORD)])
    assert repo.handled_comment_ids("c1") == set() and repo.counts("c1") == {"queued": 1, "no_keyword": 1}
    assert repo.claim(_action("k1", "u1", ActionState.PENDING))  # queued -> pending
    assert not repo.claim(_action("k1", "u1", ActionState.PENDING))  # final now
    repo.sync([_action("k1", "u1", ActionState.QUEUED)])  # never overwrites a final row
    assert repo.counts("c1") == {"pending": 1, "no_keyword": 1} and repo.handled_comment_ids("c1") == {"k1"}
