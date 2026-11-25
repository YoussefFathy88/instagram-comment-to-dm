"""Local HTML report of a comment-to-DM campaign: every comment per video and what happened to it.

Written to data/reports/ (gitignored: it holds followers' usernames). Opened locally, never published.
"""

import html
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.engagement import PRIVATE_REPLY_WINDOW, ActionState, Campaign, EngagementAction

# state -> (label, group). Groups drive the filter buttons; "you" = needs you.
STATES: dict[ActionState, tuple[str, str]] = {
    ActionState.DONE: ("Sent by engine: DM + reply", "sent"),
    ActionState.DM_ONLY: ("Sent by engine: DM only", "sent"),
    ActionState.SKIPPED_HISTORY: ("Found already sent (your DM)", "already"),
    ActionState.SKIPPED_SAME_USER: ("Already sent (same person, other comment)", "already"),
    ActionState.QUEUED: ("Waiting for a batch", "waiting"),
    ActionState.DM_FAILED: ("DM failed: send by hand", "you"),
    ActionState.SKIPPED_EXPIRED: ("Expired (7 days): send by hand", "you"),
    ActionState.PENDING: ("Interrupted: next run checks the chat", "you"),
    ActionState.NO_KEYWORD: ("No keyword", "other"),
}
GROUPS = [
    ("all", "All"),
    ("sent", "a. Sent by engine"),
    ("already", "b. Found already sent"),
    ("you", "c. Needs you"),
    ("waiting", "Waiting"),
    ("other", "No keyword"),
]


def summarize(actions: list[EngagementAction]) -> dict[str, dict[str, int]]:
    """{media_id: {state: count}}"""
    out: dict[str, Counter] = {}
    for a in actions:
        out.setdefault(a.media_id, Counter())[a.state.value] += 1
    return {m: dict(c) for m, c in out.items()}


def write_report(campaign: Campaign, actions: list[EngagementAction], out_dir: Path, tz: str, now: datetime) -> Path:
    zone = ZoneInfo(tz)
    fmt = lambda d: d.astimezone(zone).strftime("%a %d %b %H:%M") if d else ""  # noqa: E731
    e = html.escape
    sections = []
    for media_id in campaign.media_ids:
        rows = sorted((a for a in actions if a.media_id == media_id), key=lambda a: a.comment_at)
        counts = Counter(STATES[a.state][1] for a in rows)
        link = campaign.media_links.get(media_id) or ""
        title = f'<a href="{e(link)}" target="_blank" rel="noopener">{e(link)}</a>' if link else e(media_id)
        chips = "".join(
            f'<span class="chip g-{g}">{label}: <b>{counts.get(g, 0)}</b></span>' for g, label in GROUPS[1:]
        )
        body = []
        for i, a in enumerate(rows, 1):
            label, group = STATES[a.state]
            deadline = a.comment_at + PRIVATE_REPLY_WINDOW
            hours_left = (deadline - now).total_seconds() / 3600
            due = f"{fmt(deadline)} ({hours_left:.0f} h left)" if a.state is ActionState.QUEUED else ""
            dm_at = fmt(a.updated_at) if a.state in (ActionState.DONE, ActionState.DM_ONLY) else ""
            body.append(
                f'<tr data-g="{group}"><td>{i}</td>'
                f'<td><a href="https://www.instagram.com/{e(a.username)}/" target="_blank" rel="noopener">@{e(a.username)}</a></td>'
                f'<td class="c">{e(a.comment_text)}</td><td>{fmt(a.comment_at)}</td>'
                f'<td><span class="st g-{group}">{label}</span></td><td>{dm_at}</td><td>{due}</td>'
                f'<td class="c">{e(a.reply_text or "")}</td><td class="c muted">{e(a.error or "")}</td></tr>'
            )
        sections.append(
            f'<section><h2>{title}</h2><div class="chips">{chips}</div><div class="scroll"><table>'
            "<thead><tr><th>#</th><th>User</th><th>Comment</th><th>Commented</th><th>Status</th><th>DM sent</th>"
            "<th>DM deadline</th><th>Public reply</th><th>Note</th></tr></thead>"
            f'<tbody>{"".join(body) or "<tr><td colspan=9>No comments recorded yet: run engage-report --sync</td></tr>"}</tbody>'
            "</table></div></section>"
        )
    buttons = "".join(f'<button data-f="{g}"{" class=on" if g == "all" else ""}>{label}</button>' for g, label in GROUPS)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(campaign.name)} report</title>
<style>
:root{{--bg:#fff;--fg:#1b1b1f;--muted:#6b6b76;--line:#e4e4ea;--card:#f7f7f9;--sent:#1f7a4d;--already:#1d5fb4;--you:#b42318;--waiting:#8a5a00;--other:#6b6b76}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121216;--fg:#ececf1;--muted:#a3a3ad;--line:#2a2a31;--card:#1b1b21;--sent:#4cc38a;--already:#6aa8ff;--you:#ff6b5e;--waiting:#e3b341;--other:#a3a3ad}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}} h1{{margin:0 0 4px;font-size:1.5rem}} h2{{font-size:1rem;margin:24px 0 8px;word-break:break-all}}
.muted{{color:var(--muted)}} .chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}}
.chip{{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 10px}}
.bar{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}} button{{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}}
button.on{{border-color:var(--fg);font-weight:600}} .scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:900px}} th,td{{text-align:start;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{position:sticky;top:0;background:var(--bg)}} td.c{{max-width:260px;unicode-bidi:plaintext}} a{{color:inherit}}
.st{{font-weight:600}} .g-sent{{color:var(--sent)}} .g-already{{color:var(--already)}} .g-you{{color:var(--you)}} .g-waiting{{color:var(--waiting)}} .g-other{{color:var(--other)}}
</style></head><body><main>
<h1>{e(campaign.name)}</h1>
<p class="muted">Generated {fmt(now)} ({e(tz)}). Status: {e(campaign.status.value)}. Keywords: {e(", ".join(campaign.keywords))}.
"Needs you" = the engine can't send it (DM refused, 7 days passed, or a run was interrupted): send it by hand.</p>
<div class="bar">{buttons}</div>
{"".join(sections)}
</main><script>
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.bar button').forEach(x=>x.classList.toggle('on',x===b));
  const f=b.dataset.f; document.querySelectorAll('tbody tr[data-g]').forEach(r=>r.hidden=!(f==='all'||r.dataset.g===f));
}});
</script></body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{campaign.name}.html"
    path.write_text(page, encoding="utf-8")
    return path
