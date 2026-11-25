"""One-time setup helpers: read the account id from Instagram and (only with write=True) save it to .env.

Read-only unless `write` is set. Never returns or prints token values.
"""

import re
from pathlib import Path
from typing import Any

import httpx

from app.domain.errors import ConfigMissing, DomainError
from app.infrastructure.config import Redactor, Settings

def update_env(env_path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in .env, keeping every other line (and comments) as is."""
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        text = pattern.sub(lambda _: line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    env_path.write_text(text, encoding="utf-8")


def setup_instagram(settings: Settings, redact: Redactor, env_path: Path, write: bool) -> dict[str, Any]:
    """Read the Instagram account behind INSTAGRAM_ACCESS_TOKEN; with write=True save INSTAGRAM_USER_ID."""
    token = settings.instagram_access_token.get_secret_value()
    if not token:
        raise ConfigMissing(["INSTAGRAM_ACCESS_TOKEN"])
    url = f"{settings.instagram_graph_host.rstrip('/')}/{settings.meta_graph_version}/me"
    r = httpx.get(url, params={"fields": "user_id,username,account_type"}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", {})
        raise DomainError(redact(f"Instagram API error {err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"))
    user_id = str(body.get("user_id") or "")
    if write and user_id:
        update_env(env_path, {"INSTAGRAM_USER_ID": user_id})
    return {
        "username": body.get("username"),
        "account_type": body.get("account_type"),
        "user_id": user_id,
        "written": bool(write and user_id),
    }
