"""Explicit local configuration; credentials stay out of state and traces."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    name: str = ""
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    timeout: float = Field(default=60, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    input_price: Decimal | None = Field(default=None, ge=0)
    cached_input_price: Decimal | None = Field(default=None, ge=0)
    output_price: Decimal | None = Field(default=None, ge=0)

    @classmethod
    def from_env(cls) -> ModelSettings:
        return cls(
            name=os.getenv("DESK_MODEL", ""),
            base_url=os.getenv("DESK_MODEL_BASE_URL", ""),
            api_key=SecretStr(
                os.getenv("DESK_MODEL_API_KEY")
                or os.getenv(os.getenv("DESK_MODEL_API_KEY_ENV", ""), "")
            ),
            timeout=float(os.getenv("DESK_MODEL_TIMEOUT", "60")),
            max_tokens=int(os.getenv("DESK_MODEL_MAX_TOKENS", "4096")),
            input_price=os.getenv("DESK_INPUT_USD_PER_MILLION") or None,
            cached_input_price=os.getenv("DESK_CACHED_INPUT_USD_PER_MILLION") or None,
            output_price=os.getenv("DESK_OUTPUT_USD_PER_MILLION") or None,
        )

    def require_live(self) -> None:
        missing = [
            key
            for key, value in (
                ("DESK_MODEL", self.name),
                ("DESK_MODEL_BASE_URL", self.base_url),
                ("DESK_MODEL_API_KEY", self.api_key.get_secret_value()),
            )
            if not value
        ]
        if missing:
            raise ValueError("Configure " + ", ".join(missing) + " for a live model run.")
        validate_url(self.base_url)

    def build(self):
        import httpx
        from langchain_openai import ChatOpenAI

        self.require_live()
        trust_env = urlsplit(self.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
        return ChatOpenAI(
            model=self.name,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
            max_retries=0,
            temperature=0,
            use_responses_api=False,
            model_kwargs={"parallel_tool_calls": False},
            http_client=httpx.Client(timeout=self.timeout, trust_env=trust_env),
            http_async_client=httpx.AsyncClient(timeout=self.timeout, trust_env=trust_env),
        )


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use an absolute HTTP(S) service URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Keep credentials, query parameters and fragments out of service URLs.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote services require HTTPS.")
    return value.rstrip("/")


class RedmineSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    url: str = "http://127.0.0.1:3080"
    api_key: SecretStr = SecretStr("")
    project_id: int = Field(default=1, gt=0)
    timeout: float = Field(default=15, gt=0)

    _url = field_validator("url")(validate_url)

    @classmethod
    def from_env(cls) -> RedmineSettings:
        return cls(
            url=os.getenv("DESK_REDMINE_URL", "http://127.0.0.1:3080"),
            api_key=SecretStr(os.getenv("DESK_REDMINE_API_KEY", "")),
            project_id=int(os.getenv("DESK_PROJECT_ID", "1")),
        )


def state_dir() -> Path:
    path = Path(os.getenv("DESK_STATE_DIR", ".runtime")).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path
