"""Settings from the project-root .env. Secrets are SecretStr and never printed."""

import os
import re
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(os.environ.get("COMMENT_DM_ROOT", Path(__file__).resolve().parents[3]))

# Settings each feature needs. Names are .env variable names.
REQUIRED: dict[str, list[str]] = {
    "instagram_engage": ["INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"],
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    timezone: str = "UTC"
    database_path: Path = Path("./data/comment-dm.db")
    audit_log_path: Path = Path("./logs/engagement.jsonl")
    meta_graph_version: str = "v26.0"

    # Comment-to-DM engagement (Instagram API with Instagram Login)
    instagram_user_id: str = ""
    instagram_access_token: SecretStr = SecretStr("")
    instagram_graph_host: str = "https://graph.instagram.com"
    engage_delay_min_s: float = 8
    engage_delay_max_s: float = 25
    # Meta app-level rate limit = 200 calls/hour x app users (1). Keep each hourly run well under it.
    engage_max_calls_per_run: int = 170

    def path(self, p: Path) -> Path:
        """Resolve a configured path relative to the project root."""
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    def missing(self, feature: str) -> list[str]:
        out = []
        for name in REQUIRED[feature]:
            value = getattr(self, name.lower())
            raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
            if not raw.strip():
                out.append(name)
        return out

    def secret_values(self) -> list[str]:
        values = []
        for name, field in type(self).model_fields.items():
            v = getattr(self, name)
            if isinstance(v, SecretStr) and v.get_secret_value():
                values.append(v.get_secret_value())
        return values


_TOKENISH = re.compile(r"(access_token=|Bearer |OAuth )[^\s&\"']+", re.IGNORECASE)


class Redactor:
    """Removes known secrets and token-looking substrings from any text that may be shown or logged."""

    def __init__(self, secrets: list[str]):
        self.secrets = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        text = _TOKENISH.sub(lambda m: m.group(1) + "***", text)
        return re.sub(r"\bIG(?:AA|QV)[A-Za-z0-9_-]{40,}", "***", text)  # Instagram user tokens
