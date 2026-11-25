"""Control board API over a real SQLite DB, with fake Instagram/scheduler. No network, no Windows tasks."""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import get_container, get_jobs
from app.api.jobs import JobManager
from app.api.main import create_app
from app.application.services.engagement_service import EngagementSettings
from app.application.services.schedule_service import ScheduleService
from app.bootstrap import Container
from app.domain.engagement import IgAccount, IgComment
from app.infrastructure.config import Settings
from tests.fakes import NOW


class FakeIg:
    def __init__(self):
        self.dms = []

    def account(self):
        return IgAccount("me", "owner")

    def recent_media(self, limit):
        return []

    def comments(self, media_id, limit):
        return [IgComment(str(i), media_id, "skill", NOW - timedelta(hours=10 - i), f"u{i}", f"user{i}") for i in range(3)]

    def private_reply(self, comment_id, text):
        self.dms.append(comment_id)
        return "mid"

    def reply_to_comment(self, comment_id, text):
        return "rid"

    def messages_from_us(self, user_id):
        return []


class FakeScheduler:
    def __init__(self):
        self.state = {"installed": False, "task": "t"}

    def install(self, every_minutes):
        self.installs = getattr(self, "installs", 0) + 1
        self.state = {"installed": True, "task": "t", "every_minutes": every_minutes}
        return self.state

    def remove(self):
        self.state = {"installed": False, "task": "t"}
        return self.state

    def status(self):
        return self.state


@pytest.fixture
def client(tmp_path, monkeypatch):
    ig, sched = FakeIg(), FakeScheduler()
    settings = Settings(_env_file=None, database_path=tmp_path / "db.sqlite", audit_log_path=tmp_path / "log.jsonl",
                        instagram_user_id="me", instagram_access_token=SecretStr("x" * 20))

    def make() -> Container:
        c = Container(settings)
        c.__dict__["instagram_engagement"] = ig
        c.__dict__["schedule_service"] = ScheduleService(sched)
        c.engagement_service.settings = EngagementSettings(delay_s=(0, 0), sleep=lambda s: None)
        c.engagement_service.clock.now = lambda: NOW
        return c

    shared = make()
    shared.engagement_service.save({"name": "fit", "media": ["111"], "keywords": ["skill"], "dm_message": "hi", "replies": ["a", "b"]})
    app = create_app()
    app.dependency_overrides[get_container] = lambda: shared
    jobs = JobManager(container_factory=make)
    app.dependency_overrides[get_jobs] = lambda: jobs
    with TestClient(app) as tc:
        tc.ig, tc.jobs, tc.sched = ig, jobs, sched
        yield tc


def wait(tc):
    for _ in range(100):
        state = tc.get("/api/engagement/job").json()
        if state["job"] and not state["job"]["running"]:
            return state["job"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_control_board_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Comment → DM control board" in r.text


def test_live_batch_needs_approval_then_runs_and_updates_counts(client):
    assert client.post("/api/engagement/campaigns/fit/run", json={"max_actions": 2}).status_code == 409
    assert client.post("/api/engagement/campaigns/fit/approve").json()["approved"]
    r = client.post("/api/engagement/campaigns/fit/run", json={"max_actions": 2})
    assert r.status_code == 202
    job = wait(client)
    assert job["error"] is None and job["result"]["counts"] == {"done": 2}
    assert client.get("/api/engagement/campaigns/fit").json()["ledger"] == {"done": 2, "queued": 1}
    assert len(client.get("/api/engagement/campaigns/fit/actions").json()) == 3 and len(client.ig.dms) == 2
    runs = client.get("/api/engagement/campaigns/fit/runs").json()
    assert runs[0]["trigger"] == "board" and runs[0]["counts"] == {"done": 2} and runs[0]["aborted"] is None


def test_dry_run_and_sync_send_nothing(client):
    client.post("/api/engagement/campaigns/fit/run", json={"dry_run": True})
    assert wait(client)["result"]["counts"] == {"would_send": 3}
    client.post("/api/engagement/campaigns/fit/sync")
    assert wait(client)["kind"] == "sync" and client.ig.dms == []
    assert client.get("/api/engagement/campaigns/fit").json()["ledger"] == {"queued": 3}


def test_scheduler_enable_disable(client):
    assert client.post("/api/engagement/scheduler", json={"enabled": True, "every_minutes": 60}).json()["installed"]
    assert client.get("/api/engagement/scheduler").json()["every_minutes"] == 60
    assert client.post("/api/engagement/scheduler", json={"enabled": True, "every_minutes": 7}).status_code == 400
    assert not client.post("/api/engagement/scheduler", json={"enabled": False}).json()["installed"]


def test_pause_and_unknown_campaign(client):
    assert client.post("/api/engagement/campaigns/fit/pause").json()["status"] == "paused"
    assert client.get("/api/engagement/campaigns/nope").status_code == 404


def test_manual_batch_restarts_the_scheduler_clock_but_dry_run_doesnt(client):
    client.post("/api/engagement/campaigns/fit/approve")
    client.post("/api/engagement/scheduler", json={"enabled": True, "every_minutes": 60})
    assert client.sched.installs == 1
    client.post("/api/engagement/campaigns/fit/run", json={"dry_run": True})
    wait(client)
    assert client.sched.installs == 1
    client.post("/api/engagement/campaigns/fit/run", json={"max_actions": 1})
    job = wait(client)
    assert client.sched.installs == 2 and job["result"]["scheduler"]["every_minutes"] == 60


def test_manual_batch_leaves_a_disabled_scheduler_off(client):
    client.post("/api/engagement/campaigns/fit/approve")
    client.post("/api/engagement/campaigns/fit/run", json={"max_actions": 1})
    assert wait(client)["result"]["scheduler"] is None and not client.sched.state["installed"]
