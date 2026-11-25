"""CLI: `python -m app.cli <command>` (run from backend/). Prints one JSON object on stdout.

Exit codes: 0 ok, 1 blocked/failed, 2 usage/config error.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from app.bootstrap import Container
from app.domain.errors import ConfigMissing, DomainError
from app.infrastructure.config import PROJECT_ROOT
from app.infrastructure.instagram.token_refresh import refresh_instagram_token
from app.infrastructure.reports.engagement_report import summarize
from app.infrastructure.setup_tools import setup_instagram


def cmd_setup_instagram(c: Container, args) -> tuple[dict, int]:
    out = setup_instagram(c.settings, c.redact, PROJECT_ROOT / ".env", args.write)
    ok = bool(out["user_id"]) and out["account_type"] in ("BUSINESS", "MEDIA_CREATOR")
    return {"ok": ok, **out}, 0 if ok else 1


def cmd_board_autostart(c: Container, args) -> tuple[dict, int]:
    from app.infrastructure import scheduler  # Windows-only helper

    if args.off:
        return {"ok": True, **scheduler.remove_board_autostart()}, 0
    if args.on:
        return {"ok": True, **scheduler.install_board_autostart(PROJECT_ROOT / "scripts" / "control-board.ps1")}, 0
    return {"ok": True, **scheduler.status(scheduler.BOARD_TASK_NAME)}, 0


# ---------------------------------------------------------------- comment-to-DM engagement


def cmd_engage_media(c: Container, args) -> tuple[dict, int]:
    ig = c.instagram_engagement
    media = ig.recent_media(args.limit)
    return {
        "ok": True,
        "account": ig.account().username,
        "media": [
            {"id": m.id, "type": m.product_type, "posted": m.timestamp.isoformat(), "link": m.permalink, "caption": m.caption[:80]}
            for m in media
        ],
    }, 0


def cmd_engage_save(c: Container, args) -> tuple[dict, int]:
    path = Path(args.file)
    if not path.is_absolute() and not path.exists():
        path = PROJECT_ROOT / path
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    campaign = c.engagement_service.save(spec)
    out = {"ok": True, **campaign.to_dict()}
    if not campaign.approved:
        out["next"] = f"Dry run: engage-run --campaign {campaign.id} --dry-run, then after your yes: engage-approve {campaign.id}"
    return out, 0


def cmd_engage_list(c: Container, args) -> tuple[dict, int]:
    return {"ok": True, "campaigns": [x.to_dict() for x in c.engagement_service.list()]}, 0


def cmd_engage_approve(c: Container, args) -> tuple[dict, int]:
    return {"ok": True, **c.engagement_service.approve(args.campaign).to_dict()}, 0


def cmd_engage_pause(c: Container, args) -> tuple[dict, int]:
    return {"ok": True, **c.engagement_service.pause(args.campaign).to_dict()}, 0


def _token_state(c: Container) -> Path:
    return c.settings.path(c.settings.database_path).parent / "instagram_token.json"


def cmd_engage_run(c: Container, args) -> tuple[dict, int]:
    """Manual ("next batch") and scheduled (--auto) runs use the same engine and the same lock."""
    out: dict[str, Any] = {"dry_run": args.dry_run}
    if args.auto:
        # Renew the 60-day token weekly; a failure is reported but doesn't block this run (token still valid).
        out["token"] = refresh_instagram_token(c.settings, c.redact, PROJECT_ROOT / ".env", _token_state(c))
    service = c.engagement_service
    if args.auto and not args.dry_run:
        # Backup for the clock reset below: a manual batch ended recently → skip this tick (rate limit, no burst).
        every = c.schedule_service.status().get("every_minutes") or 60
        since = service.minutes_since_manual_run()
        if since is not None and since < every * 0.75:
            return {"ok": True, **out, "skipped": f"a manual batch ended {since:.0f} min ago; next scheduled run continues"}, 0
    with c.engagement_lock():
        if args.campaign:
            trigger = "scheduler" if args.auto else "cli"
            reports = [service.run(args.campaign, dry_run=args.dry_run, max_actions=args.max, only_username=args.only_user, trigger=trigger)]
        elif args.only_user:
            return {"ok": False, "error": "--only-user needs --campaign"}, 2
        else:
            reports = service.run_active(dry_run=args.dry_run, max_actions=args.max, trigger="scheduler" if args.auto else "cli")
    ok = all(r.ok for r in reports)
    if not args.dry_run:  # keep each campaign's report in step with the database
        out["reports"] = [str(_write_report(c, r.campaign_id)) for r in reports]
    if not args.dry_run and not args.auto and reports:
        out["scheduler"] = _restart_schedule(c)
    return {"ok": ok, **out, "runs": [r.to_dict() for r in reports]}, 0 if ok else 1


def _restart_schedule(c: Container) -> dict[str, Any] | str | None:
    """A manual batch restarts the scheduler clock (next run = one interval after it). Never fails the run."""
    try:
        return c.schedule_service.restart_after_manual_run()
    except Exception as e:
        return c.redact(f"couldn't restart the scheduler clock: {e}")


def _write_report(c: Container, campaign_id: str) -> Path:
    return c.write_engagement_report(campaign_id)


def cmd_engage_report(c: Container, args) -> tuple[dict, int]:
    """Per-video status of every comment. --sync first reads Instagram and updates the database (sends nothing)."""
    targets = [c.engagement_service.list_one(args.campaign)] if args.campaign else c.engagement_service.list()
    out = []
    for campaign in targets:
        synced = c.engagement_service.sync(campaign.id).to_dict() if args.sync else None
        _, actions = c.engagement_service.report(campaign.id)
        out.append({
            "campaign": campaign.name,
            "synced": None if synced is None else {k: synced[k] for k in ("ok", "scanned", "matched", "aborted")},
            "per_video": summarize(actions),
            "report": str(_write_report(c, campaign.id)),
        })
    return {"ok": True, "campaigns": out}, 0


def cmd_engage_status(c: Container, args) -> tuple[dict, int]:
    campaigns = [c.engagement_service.status(x.id) for x in c.engagement_service.list()]
    state = _token_state(c)
    token = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {"refreshed_at": None}
    return {"ok": True, "campaigns": campaigns, "scheduler": c.schedule_service.status(), "instagram_token": token}, 0


def cmd_engage_forget(c: Container, args) -> tuple[dict, int]:
    return {"ok": True, "rows_deleted": c.engagement_service.forget(args.username)}, 0


def cmd_engage_refresh_token(c: Container, args) -> tuple[dict, int]:
    out = refresh_instagram_token(c.settings, c.redact, PROJECT_ROOT / ".env", _token_state(c), force=True)
    return {"ok": out["refreshed"], **out}, 0 if out["refreshed"] else 1


def cmd_engage_schedule(c: Container, args) -> tuple[dict, int]:
    if args.off:
        return {"ok": True, **c.schedule_service.disable()}, 0
    if args.every:
        return {"ok": True, **c.schedule_service.enable(args.every)}, 0
    return {"ok": True, **c.schedule_service.status()}, 0


def cmd_engage_history(c: Container, args) -> tuple[dict, int]:
    actions = c.engagement_service.history(args.campaign, args.limit)
    return {"ok": True, "actions": [a.to_dict() for a in actions]}, 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Comment to DM: reply to keyword comments on Instagram and send a private DM")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup-instagram", help="Check INSTAGRAM_ACCESS_TOKEN's account; --write saves INSTAGRAM_USER_ID")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_setup_instagram)

    s = sub.add_parser("engage-media", help="Newest Instagram posts/reels (ids + links) to target a campaign")
    s.add_argument("--limit", type=int, default=12)
    s.set_defaults(fn=cmd_engage_media)

    s = sub.add_parser("engage-save", help="Create/update a comment-to-DM campaign from a YAML file (edits drop approval)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_engage_save)

    sub.add_parser("engage-list", help="Comment-to-DM campaigns and their status").set_defaults(fn=cmd_engage_list)

    s = sub.add_parser("engage-approve", help="Approve a campaign's exact content for live runs (only after you decide)")
    s.add_argument("campaign", help="campaign id or name")
    s.set_defaults(fn=cmd_engage_approve)

    s = sub.add_parser("engage-pause", help="Stop live runs for a campaign")
    s.add_argument("campaign")
    s.set_defaults(fn=cmd_engage_pause)

    s = sub.add_parser("engage-run", help="Reply + DM matching comments (next batch). --dry-run writes and sends nothing")
    s.add_argument("--campaign", help="one campaign (default: every approved campaign)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max", type=int, help="batch size for this run (never above the campaign's max_actions_per_run)")
    s.add_argument("--auto", action="store_true", help="scheduled run: also renew the Instagram token when due")
    s.add_argument("--only-user", help="only this commenter's comments (live test on a tester account)")
    s.set_defaults(fn=cmd_engage_run)

    sub.add_parser("engage-status", help="Ledger counts per campaign, scheduler state, token refresh date").set_defaults(
        fn=cmd_engage_status
    )

    s = sub.add_parser("board-autostart", help="Start the control board at Windows logon: --on / --off (no flag: status)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_board_autostart)

    s = sub.add_parser("engage-report", help="HTML status of every comment per video (data/reports/). --sync reads Instagram first")
    s.add_argument("--campaign")
    s.add_argument("--sync", action="store_true", help="refresh the database from Instagram first (sends nothing)")
    s.set_defaults(fn=cmd_engage_report)

    s = sub.add_parser("engage-forget", help="Delete every record of one Instagram username (privacy request)")
    s.add_argument("username")
    s.set_defaults(fn=cmd_engage_forget)

    sub.add_parser("engage-refresh-token", help="Renew the 60-day Instagram token now (writes .env)").set_defaults(
        fn=cmd_engage_refresh_token
    )

    s = sub.add_parser("engage-schedule", help="Windows Task Scheduler job: --every MINUTES to install, --off to remove")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--every", type=int)
    g.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_engage_schedule)

    s = sub.add_parser("engage-history", help="What was sent/skipped per comment")
    s.add_argument("campaign")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_engage_history)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    container = Container()
    try:
        out, code = args.fn(container, args)
    except ConfigMissing as e:
        out, code = {"ok": False, "error": str(e), "missing": e.names}, 2
    except DomainError as e:
        out, code = {"ok": False, "error": container.redact(str(e))}, 1
    print(container.redact(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    return code


if __name__ == "__main__":
    sys.exit(main())
