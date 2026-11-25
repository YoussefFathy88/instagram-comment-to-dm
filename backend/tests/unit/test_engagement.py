"""Comment-to-DM: pure rules + EngagementService against in-memory fakes."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.application.services.engagement_service import EngagementService, EngagementSettings
from app.domain import engagement as rules
from app.domain.engagement import ActionState, Campaign, CampaignStatus, IgAccount, IgComment, IgMedia, IgMessage
from app.domain.errors import CampaignInvalid, EngagementBlocked, EngagementError
from tests.fakes import NOW, FakeAudit, FakeClock

DM = "أهلاً {username} 👋 ده لينك الـ skill: https://example.com/skill"
REPLIES = ["بعتلك DM 📩", "شوف الرسايل 🔥", "وصلك في الخاص ✅"]
ME = IgAccount("me-1", "owner")


# ---------------------------------------------------------------- rules


@pytest.mark.parametrize(
    "comment",
    ["Skill", "skills please", "SKILLLL!!", "عايز السكيل", "سكييييل", "اسكيل لو سمحت", "skill🔥", "S K I L L"[0:1] + "kill"],
)
def test_keywords_match_variants(comment):
    assert rules.matches_keywords(comment, ["skill", "سكيل"])


@pytest.mark.parametrize("comment", ["great video", "شكرا", "", "skull"])
def test_keywords_ignore_others(comment):
    assert not rules.matches_keywords(comment, ["skill", "سكيل"])


def test_arabic_normalization_alef_and_taa_marbuta():
    assert rules.matches_keywords("ابعتلي الأداة", ["الاداه"])


def test_similar_detects_same_dm_and_light_edits():
    assert rules.similar(DM, "أهلاً owner 👋 ده لينك الـ skill: https://example.com/skill")
    assert rules.similar("Here is the skill file, enjoy it!", "here is the skill file enjoy it")
    assert rules.similar("hi https://x.io/a", "totally different text but same link https://x.io/a")
    assert not rules.similar(DM, "تمام شكرا")


def test_pick_reply_avoids_recent():
    rng = random.Random(1)
    seen = []
    for _ in range(30):
        r = rules.pick_reply(REPLIES, seen, rng)
        if seen:
            assert r not in seen[-2:]
        seen.append(r)


def test_pick_reply_single_option():
    assert rules.pick_reply(["only"], ["only"], random.Random()) == "only"


def test_window():
    assert rules.within_private_reply_window(NOW - timedelta(days=6), NOW)
    assert not rules.within_private_reply_window(NOW - timedelta(days=6, hours=23, minutes=30), NOW)


def test_validate_dm_bytes_limit():
    c = campaign(dm_message="س" * 600)  # 1,200 bytes in UTF-8
    assert any("bytes" in p for p in rules.validate_campaign(c))


def test_fingerprint_changes_with_content():
    a, b = campaign(), campaign(replies=REPLIES + ["جديد"])
    assert a.fingerprint != b.fingerprint


# ---------------------------------------------------------------- fakes


def campaign(**kw) -> Campaign:
    base = dict(id="c1", name="skill-dm", media_ids=["m1"], keywords=["skill", "سكيل"], dm_message=DM, replies=REPLIES)
    base.update(kw)
    return Campaign(**base)


def comment(cid: str, text: str = "skill", user: str = "u1", age_h: float = 1, media: str = "m1", owner_replied=False) -> IgComment:
    return IgComment(cid, media, text, NOW - timedelta(hours=age_h), user, f"user_{user}", owner_replied)


class FakeInstagram:
    def __init__(
        self, comments=None, history=None, dm_errors=None, blocked_on=None, media=None,
        maybe_sent=False, reply_blocked_on=None, retry_errors=None,
    ):
        self._comments = comments or []
        self.history = history or {}
        self.dm_errors = dm_errors or {}
        self.retry_errors = retry_errors or set()
        self.blocked_on = blocked_on
        self.maybe_sent = maybe_sent
        self.reply_blocked_on = reply_blocked_on
        self.media = media or []
        self.dms: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str]] = []
        self.history_reads: list[str] = []

    def account(self):
        return ME

    def recent_media(self, limit):
        return self.media[:limit]

    def comments(self, media_id, limit):
        return [c for c in self._comments if c.media_id == media_id][:limit]

    def private_reply(self, comment_id, text):
        if comment_id == self.blocked_on:
            raise EngagementBlocked("token expired", maybe_sent=self.maybe_sent)
        if comment_id in self.retry_errors:
            raise EngagementError("Instagram API error 2/None: temporary", retryable=True)
        if comment_id in self.dm_errors:
            raise EngagementError(self.dm_errors[comment_id])
        self.dms.append((comment_id, text))
        return f"msg-{comment_id}"

    def reply_to_comment(self, comment_id, text):
        if comment_id == self.reply_blocked_on:
            raise EngagementBlocked("rate limited")
        self.replies.append((comment_id, text))
        return f"reply-{comment_id}"

    def messages_from_us(self, user_id):
        self.history_reads.append(user_id)
        return self.history.get(user_id, [])


class FakeCampaigns:
    def __init__(self, *campaigns):
        self.items = {c.id: c for c in campaigns}

    def save(self, c):
        self.items[c.id] = c
        return c

    def get(self, cid):
        if cid in self.items:
            return self.items[cid]
        for c in self.items.values():
            if c.name == cid:
                return c
        from app.domain.errors import CampaignNotFound

        raise CampaignNotFound(cid)

    def get_by_name(self, name):
        return next((c for c in self.items.values() if c.name == name), None)

    def list(self):
        return list(self.items.values())


class FakeActions:
    def __init__(self):
        self.rows = {}

    def claim(self, a):
        key = (a.campaign_id, a.comment_id)
        if key in self.rows and self.rows[key].state not in rules.OPEN_STATES:
            return False
        self.rows[key] = a
        return True

    def sync(self, actions):
        for a in actions:
            key = (a.campaign_id, a.comment_id)
            if key not in self.rows or self.rows[key].state in rules.OPEN_STATES:
                self.rows[key] = a

    def update(self, a):
        self.rows[(a.campaign_id, a.comment_id)] = a

    def handled_comment_ids(self, cid):
        return {k[1] for k, a in self.rows.items() if k[0] == cid and a.state not in rules.OPEN_STATES}

    def users_with_dm(self, cid):
        ok = {ActionState.DONE, ActionState.DM_ONLY, ActionState.SKIPPED_HISTORY, ActionState.PENDING}
        return {a.user_id for k, a in self.rows.items() if k[0] == cid and a.state in ok}

    def counts(self, cid):
        out = {}
        for k, a in self.rows.items():
            if k[0] == cid:
                out[a.state.value] = out.get(a.state.value, 0) + 1
        return out

    def forget_user(self, username):
        keys = [k for k, a in self.rows.items() if a.username.lower() == username.lower()]
        for k in keys:
            del self.rows[k]
        return len(keys)

    def recent_replies(self, cid, limit):
        return [a.reply_text for k, a in reversed(self.rows.items()) if k[0] == cid and a.reply_text][:limit]

    def list(self, cid, limit=200):
        return [a for k, a in self.rows.items() if k[0] == cid][:limit]


def service(ig, *campaigns, actions=None):
    sleeps = []
    svc = EngagementService(
        campaigns=FakeCampaigns(*campaigns),
        actions=actions or FakeActions(),
        instagram=lambda: ig,
        audit=FakeAudit(),
        clock=FakeClock(),
        settings=EngagementSettings(delay_s=(1, 2), sleep=sleeps.append, rng=random.Random(0)),
    )
    svc.sleeps = sleeps
    return svc


def approved(**kw) -> Campaign:
    c = campaign(**kw)
    c.status, c.approved_fingerprint = CampaignStatus.ACTIVE, c.fingerprint
    return c


# ---------------------------------------------------------------- service


def test_live_run_requires_approval():
    ig = FakeInstagram([comment("1")])
    report = service(ig, campaign()).run("c1", dry_run=False)
    assert not report.ok and "not approved" in report.aborted
    assert ig.dms == [] and ig.replies == []


def test_editing_content_drops_approval():
    c = approved()
    c.replies = REPLIES + ["new"]
    assert not c.approved


def test_dry_run_has_zero_side_effects():
    ig = FakeInstagram([comment("1"), comment("2", "nice", "u2")])
    actions = FakeActions()
    svc = service(ig, campaign(), actions=actions)
    report = svc.run("c1", dry_run=True)
    assert report.ok and report.matched == 1
    assert report.items[0]["state"] == "would_send"
    assert "user_u1" in report.items[0]["dm"]
    assert ig.dms == [] and ig.replies == [] and actions.rows == {} and svc.audit.events == [] and svc.sleeps == []


def test_live_run_dms_then_replies_and_records():
    ig = FakeInstagram([comment("1", user="u1"), comment("2", "سكيل", "u2"), comment("3", "wow", "u3")])
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    report = svc.run("c1", dry_run=False)
    assert report.ok and report.matched == 2
    assert [d[0] for d in ig.dms] == ["1", "2"] and len(ig.replies) == 2
    assert ig.replies[0][1] != ig.replies[1][1]  # rotation
    assert {k[1]: a.state for k, a in actions.rows.items()} == {"1": ActionState.DONE, "2": ActionState.DONE, "3": ActionState.NO_KEYWORD}
    assert len(svc.sleeps) == 1  # pause between actions, not before the first


def test_second_run_sends_nothing_again():
    ig = FakeInstagram([comment("1"), comment("2", user="u2")])
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    svc.run("c1", dry_run=False)
    report = svc.run("c1", dry_run=False)
    assert len(ig.dms) == 2 and report.already_handled == 2 and report.items == []


def test_same_user_gets_one_dm_per_campaign():
    ig = FakeInstagram([comment("1", user="u1", age_h=3), comment("2", "skill again", "u1", age_h=1)])
    actions = FakeActions()
    service(ig, approved(), actions=actions).run("c1", dry_run=False)
    assert [d[0] for d in ig.dms] == ["1"]
    assert actions.rows[("c1", "2")].state is ActionState.SKIPPED_SAME_USER


def test_skips_users_whose_conversation_has_the_campaign_link():
    hand = IgMessage("اتفضل يا باشا https://example.com/skill", NOW - timedelta(minutes=30))
    ig = FakeInstagram([comment("1", user="u1", age_h=5)], history={"u1": [hand]})
    actions = FakeActions()
    service(ig, approved(), actions=actions).run("c1", dry_run=False)
    assert ig.dms == [] and ig.replies == [] and actions.rows[("c1", "1")].state is ActionState.SKIPPED_HISTORY


def test_unrelated_chat_after_the_comment_does_not_block_the_dm():
    chat = IgMessage("hi, how are you?", NOW - timedelta(minutes=10))
    ig = FakeInstagram([comment("1", user="u1", age_h=5)], history={"u1": [chat]})
    service(ig, approved()).run("c1", dry_run=False)
    assert [d[0] for d in ig.dms] == ["1"]


def test_public_reply_by_hand_means_dm_only():
    ig = FakeInstagram([comment("1", owner_replied=True)])
    actions = FakeActions()
    service(ig, approved(), actions=actions).run("c1", dry_run=False)
    assert len(ig.dms) == 1 and ig.replies == [] and actions.rows[("c1", "1")].state is ActionState.DM_ONLY


def test_unverifiable_conversation_gets_the_apology_prefix():
    ig = FakeInstagram([comment("1")])
    ig.messages_from_us = lambda user_id: None
    actions = FakeActions()
    service(ig, approved(unverified_prefix="لو وصلتك قبل كده معلش 🙏"), actions=actions).run("c1", dry_run=False)
    assert ig.dms[0][1].startswith("لو وصلتك قبل كده معلش 🙏") and "user_u1" in ig.dms[0][1]
    assert "unverified prefix" in actions.rows[("c1", "1")].error


def test_verified_conversation_gets_no_prefix():
    ig = FakeInstagram([comment("1")])
    service(ig, approved(unverified_prefix="sorry")).run("c1", dry_run=False)
    assert not ig.dms[0][1].startswith("sorry")


def test_manual_batch_size_never_exceeds_campaign_limit():
    cs = [comment(str(i), user=f"u{i}", age_h=10 - i) for i in range(6)]
    ig = FakeInstagram(cs)
    svc = service(ig, approved(max_actions_per_run=4))
    assert svc.run("c1", dry_run=False, max_actions=2).deferred == 4 and len(ig.dms) == 2
    assert svc.run("c1", dry_run=False, max_actions=50).deferred == 0 and len(ig.dms) == 6


def test_expired_comments_are_recorded_not_sent():
    ig = FakeInstagram([comment("1", age_h=24 * 8)])
    actions = FakeActions()
    service(ig, approved(), actions=actions).run("c1", dry_run=False)
    assert ig.dms == [] and actions.rows[("c1", "1")].state is ActionState.SKIPPED_EXPIRED


def test_own_comments_ignored():
    ig = FakeInstagram([IgComment("1", "m1", "skill", NOW, ME.id, ME.username)])
    report = service(ig, approved()).run("c1", dry_run=False)
    assert report.matched == 0 and ig.dms == []


def test_dm_failure_uses_fallback_reply():
    ig = FakeInstagram([comment("1")], dm_errors={"1": "user can't receive messages"})
    actions = FakeActions()
    service(ig, approved(fallback_reply="ابعتلي رسالة وهبعتهولك"), actions=actions).run("c1", dry_run=False)
    row = actions.rows[("c1", "1")]
    assert row.state is ActionState.DM_FAILED and ig.replies == [("1", "ابعتلي رسالة وهبعتهولك")]


def test_dm_failure_without_fallback_posts_nothing():
    ig = FakeInstagram([comment("1")], dm_errors={"1": "nope"})
    service(ig, approved()).run("c1", dry_run=False)
    assert ig.replies == []


def test_three_failed_dms_in_a_row_stop_the_run():
    cs = [comment(str(i), user=f"u{i}", age_h=10 - i) for i in range(5)]
    ig = FakeInstagram(cs, dm_errors={str(i): "fail" for i in range(5)})
    report = service(ig, approved()).run("c1", dry_run=False)
    assert not report.ok and len(report.items) == 3


def test_blocked_before_sending_puts_the_person_back_in_the_queue():
    ig = FakeInstagram([comment("1", age_h=2), comment("2", user="u2", age_h=1)], blocked_on="1")
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    report = svc.run("c1", dry_run=False, trigger="scheduler")
    assert not report.ok and "token" in report.aborted
    first = actions.rows[("c1", "1")]
    assert first.state is ActionState.QUEUED and "back in the queue" in first.error and "token" in first.error
    assert actions.rows[("c1", "2")].state is ActionState.QUEUED
    run = svc.runs("c1")[0]  # the root cause is logged with the run
    assert run["trigger"] == "scheduler" and "token" in run["aborted"]
    ig.blocked_on = None  # next run: token fixed → the person gets the DM
    svc.run("c1", dry_run=False)
    assert actions.rows[("c1", "1")].state is ActionState.DONE and [d[0] for d in ig.dms] == ["1", "2"]


def test_lost_answer_leaves_pending_then_next_run_finds_the_dm():
    ig = FakeInstagram([comment("1")], blocked_on="1", maybe_sent=True)
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    svc.run("c1", dry_run=False)
    assert actions.rows[("c1", "1")].state is ActionState.PENDING
    ig.blocked_on, ig.history = None, {"u1": [IgMessage(rules.render(DM, comment("1")), NOW)]}  # it did arrive
    report = svc.run("c1", dry_run=False)
    assert report.recovered == 1 and actions.rows[("c1", "1")].state is ActionState.DM_ONLY
    assert ig.dms == []  # never sent twice


def test_pending_without_dm_in_the_chat_is_sent_next_run():
    ig = FakeInstagram([comment("1")], blocked_on="1", maybe_sent=True)
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    svc.run("c1", dry_run=False)
    ig.blocked_on = None
    report = svc.run("c1", dry_run=False)
    assert report.recovered == 1 and actions.rows[("c1", "1")].state is ActionState.DONE and len(ig.dms) == 1


def test_pending_stays_when_the_chat_cant_be_read():
    ig = FakeInstagram([comment("1")], blocked_on="1", maybe_sent=True)
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    svc.run("c1", dry_run=False)
    ig.blocked_on, ig.messages_from_us = None, lambda user_id: None
    svc.run("c1", dry_run=False)
    assert actions.rows[("c1", "1")].state is ActionState.PENDING and ig.dms == []


def test_dry_run_doesnt_touch_pending_rows():
    ig = FakeInstagram([comment("1")], blocked_on="1", maybe_sent=True)
    actions = FakeActions()
    svc = service(ig, approved(), actions=actions)
    svc.run("c1", dry_run=False)
    ig.blocked_on, events = None, len(svc.audit.events)
    svc.run("c1", dry_run=True)
    assert actions.rows[("c1", "1")].state is ActionState.PENDING and len(svc.audit.events) == events


def test_dm_sent_but_reply_blocked_is_recorded_as_sent():
    ig = FakeInstagram([comment("1")], reply_blocked_on="1")
    actions = FakeActions()
    report = service(ig, approved(), actions=actions).run("c1", dry_run=False)
    row = actions.rows[("c1", "1")]
    assert not report.ok and row.state is ActionState.DM_ONLY and row.dm_message_id == "msg-1"
    assert "public reply not posted" in row.error


def test_temporary_instagram_error_requeues_without_fallback_reply():
    ig = FakeInstagram([comment("1", age_h=2), comment("2", user="u2", age_h=1)], retry_errors={"1"})
    actions = FakeActions()
    svc = service(ig, approved(fallback_reply="ابعتلي رسالة"), actions=actions)
    svc.run("c1", dry_run=False)
    row = actions.rows[("c1", "1")]
    assert row.state is ActionState.QUEUED and "temporary" in row.error and ig.replies == [("2", ig.replies[0][1])]
    ig.retry_errors = set()
    svc.run("c1", dry_run=False)
    assert actions.rows[("c1", "1")].state is ActionState.DONE


def test_crash_is_logged_with_the_run():
    ig = FakeInstagram([comment("1")])
    ig.private_reply = lambda *a: 1 / 0
    svc = service(ig, approved())
    with pytest.raises(ZeroDivisionError):
        svc.run("c1", dry_run=False)
    assert "crashed: ZeroDivisionError" in svc.runs("c1")[0]["aborted"]


def test_refused_run_is_logged():
    svc = service(FakeInstagram([comment("1")]), campaign())
    svc.run("c1", dry_run=False, trigger="board")
    run = svc.runs("c1")[0]
    assert run["trigger"] == "board" and "not approved" in run["aborted"]


def test_max_actions_per_run_defers_the_rest():
    cs = [comment(str(i), user=f"u{i}", age_h=10 - i) for i in range(5)]
    ig = FakeInstagram(cs)
    report = service(ig, approved(max_actions_per_run=2)).run("c1", dry_run=False)
    assert len(ig.dms) == 2 and report.deferred == 3
    assert [d[0] for d in ig.dms] == ["0", "1"]  # oldest first (closest to the 7-day limit)


def test_claim_conflict_skips():
    ig = FakeInstagram([comment("1")])
    actions = FakeActions()
    actions.claim = lambda a: False
    report = service(ig, approved(), actions=actions).run("c1", dry_run=False)
    assert ig.dms == [] and report.already_handled == 1


def test_save_resolves_links_and_latest_and_keeps_approval_when_unchanged():
    media = [
        IgMedia("111", "https://www.instagram.com/reel/AbC1/", "", NOW, "REELS"),
        IgMedia("222", "https://www.instagram.com/p/XyZ2/", "", NOW, "FEED"),
    ]
    ig = FakeInstagram(media=media)
    svc = service(ig)
    spec = {"name": "n", "media": ["https://www.instagram.com/p/XyZ2/?igsh=abc", "latest", "333"], "keywords": ["skill"], "dm_message": "hi", "replies": ["a"]}
    c = svc.save(spec)
    assert c.media_ids == ["222", "111", "333"] and c.status is CampaignStatus.DRAFT
    assert c.media_links["222"] == "https://www.instagram.com/p/XyZ2/"
    svc.approve(c.id)
    assert svc.save(spec).approved
    spec["dm_message"] = "changed"
    assert not svc.save(spec).approved


def test_save_rejects_invalid_and_unknown_links():
    svc = service(FakeInstagram())
    with pytest.raises(CampaignInvalid):
        svc.save({"name": "n", "media": ["1"], "keywords": [], "dm_message": "x", "replies": ["a"]})
    with pytest.raises(CampaignInvalid):
        svc.save({"name": "n", "media": ["https://www.instagram.com/reel/nope/"], "keywords": ["k"], "dm_message": "x", "replies": ["a"]})


def test_run_active_only_runs_approved():
    ig = FakeInstagram([comment("1")])
    svc = service(ig, approved(), campaign(id="c2", name="draft"))
    reports = svc.run_active(dry_run=False)
    assert [r.campaign_id for r in reports] == ["c1"]


def test_status_and_forget():
    ig = FakeInstagram([comment("1"), comment("2", user="u2")])
    svc = service(ig, approved())
    svc.run("c1", dry_run=False)
    assert svc.status("c1")["ledger"] == {"done": 2}
    assert svc.forget("@user_u1") == 1 and svc.status("c1")["ledger"] == {"done": 1}
    assert "user_u1" not in str(svc.audit.events[-1])


def test_only_username_limits_the_run_to_one_commenter():
    ig = FakeInstagram([comment("1", user="u1"), comment("2", user="tester")])
    report = service(ig, approved()).run("c1", dry_run=False, only_username="@USER_tester")
    assert [d[0] for d in ig.dms] == ["2"] and report.matched == 1


def test_live_run_writes_every_comment_to_the_database():
    cs = [comment(str(i), user=f"u{i}", age_h=10 - i) for i in range(4)] + [comment("x", "🔥", "fan")]
    ig = FakeInstagram(cs)
    actions = FakeActions()
    svc = service(ig, approved(max_actions_per_run=2), actions=actions)
    svc.run("c1", dry_run=False)
    states = {k[1]: a.state for k, a in actions.rows.items()}
    assert states == {"0": ActionState.DONE, "1": ActionState.DONE, "2": ActionState.QUEUED, "3": ActionState.QUEUED,
                      "x": ActionState.NO_KEYWORD}
    svc.run("c1", dry_run=False)  # next batch picks up the queued ones
    assert {k[1]: a.state for k, a in actions.rows.items()}["3"] is ActionState.DONE and len(ig.dms) == 4


def test_sync_refreshes_the_queue_without_sending():
    ig = FakeInstagram([comment("1"), comment("2", "nice", "u2")])
    actions = FakeActions()
    report = service(ig, campaign(), actions=actions).sync("c1")  # draft is fine: nothing is sent
    assert ig.dms == [] and report.matched == 1
    assert {k[1]: a.state for k, a in actions.rows.items()} == {"1": ActionState.QUEUED, "2": ActionState.NO_KEYWORD}


def test_dry_run_still_writes_nothing():
    ig = FakeInstagram([comment("1")])
    actions = FakeActions()
    service(ig, approved(), actions=actions).run("c1", dry_run=True)
    assert actions.rows == {}


# ---------------------------------------------------------------- cross-campaign safety

SKILL_DM = "شكرا علي تعليقك متنساش تعمل Follow\nFitness skill:\nhttps://drive.google.com/drive/folders/1IpjOSQcUxs5YvE3yVJyVqts9C56_gNCw?usp=sharing"
PROMPT_DM = SKILL_DM.replace("Fitness skill:", "Prompt:").replace("1IpjOSQcUxs5YvE3yVJyVqts9C56_gNCw", "1ZZZotherFolderId99")


def test_same_template_different_link_is_not_already_sent():
    assert not rules.similar(PROMPT_DM, SKILL_DM)
    assert not rules.similar(PROMPT_DM, SKILL_DM.replace("Fitness skill:", "Prompt:"))  # even with identical text


def test_same_drive_folder_in_any_link_form_counts():
    folder = "1IpjOSQcUxs5YvE3yVJyVqts9C56_gNCw"
    for hand in (
        f"اتفضل https://drive.google.com/drive/folders/{folder}?usp=drive_link",
        f"https://drive.google.com/drive/u/0/folders/{folder}",
        f"(https://drive.google.com/drive/folders/{folder}).",
    ):
        assert rules.similar(SKILL_DM, hand), hand
    assert rules.similar("see https://example.com/a/", "here https://www.example.com/a?x=1")


def test_person_dmed_in_an_old_campaign_still_gets_the_new_one():
    old_dm = IgMessage(SKILL_DM, NOW - timedelta(days=20))
    ig = FakeInstagram([comment("1", "prompt please", "u1")], history={"u1": [old_dm]})
    actions = FakeActions()
    actions.rows[("old", "x")] = rules.EngagementAction("old", "x", "m0", "u1", "user_u1", "skill", NOW, ActionState.DONE)
    service(ig, approved(keywords=["prompt"], dm_message=PROMPT_DM), actions=actions).run("c1", dry_run=False)
    assert [d[0] for d in ig.dms] == ["1"]  # old campaign's DM (other link) and old ledger row don't block it


def test_scan_limit_reached_is_a_warning_in_the_run_log():
    ig = FakeInstagram([comment(str(i), user=f"u{i}", age_h=i + 1) for i in range(3)])
    svc = service(ig, approved(scan_limit=3))
    report = svc.run("c1", dry_run=False)
    assert report.ok and "scan_limit 3" in report.warnings[0]
    assert "scan_limit 3" in svc.runs("c1")[0]["warnings"][0]


def test_minutes_since_manual_run_ignores_scheduled_runs():
    svc = service(FakeInstagram([comment("1")]), approved())
    assert svc.minutes_since_manual_run() is None
    svc.run("c1", dry_run=False, trigger="scheduler")
    assert svc.minutes_since_manual_run() is None
    svc.audit.events[-1]["at"] = NOW.isoformat()
    svc.audit.append({"event": "engagement_run", "trigger": "board", "dry_run": False, "at": (NOW - timedelta(minutes=20)).isoformat()})
    assert svc.minutes_since_manual_run() == 20
