"""Keep the 60-day Instagram user token alive by refreshing it about once a week.

GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=...
Token must be at least 24 h old and not expired; the new one is valid 60 days.
https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token (checked 2026-10-04)
The endpoint takes the token as a query parameter; every message is redacted before it's returned.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.infrastructure.config import Redactor, Settings
from app.infrastructure.setup_tools import update_env

REFRESH_EVERY = timedelta(days=7)


def refresh_instagram_token(
    settings: Settings, redact: Redactor, env_path: Path, state_path: Path, force: bool = False, http: httpx.Client | None = None
) -> dict[str, Any]:
    """Refresh when due (or forced). Writes the new token to .env and the refresh time to state_path."""
    token = settings.instagram_access_token.get_secret_value()
    if not token:
        return {"refreshed": False, "reason": "INSTAGRAM_ACCESS_TOKEN is empty"}
    now = datetime.now(timezone.utc)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    last = datetime.fromisoformat(state["refreshed_at"]) if state.get("refreshed_at") else None
    if not force and last and now - last < REFRESH_EVERY:
        return {"refreshed": False, "reason": f"not due; last refresh {last.date().isoformat()}"}

    client = http or httpx.Client(timeout=30)
    url = f"{settings.instagram_graph_host.rstrip('/')}/refresh_access_token"
    try:
        r = client.get(url, params={"grant_type": "ig_refresh_token", "access_token": token})
        body = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as e:
        return {"refreshed": False, "reason": f"refresh failed: {type(e).__name__}"}
    new = body.get("access_token")
    if r.status_code >= 400 or not new:
        err = body.get("error", {})
        return {"refreshed": False, "reason": redact(f"refresh failed {err.get('code')}: {err.get('message')}")}

    update_env(env_path, {"INSTAGRAM_ACCESS_TOKEN": new})
    expires_at = now + timedelta(seconds=int(body.get("expires_in", 0)))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"refreshed_at": now.isoformat(), "expires_at": expires_at.isoformat()}), encoding="utf-8")
    return {"refreshed": True, "expires_at": expires_at.isoformat()}
