from __future__ import annotations

import os
import socket
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from servicedesk_agent.config import ModelSettings, RedmineSettings, state_dir, validate_url


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("DESK_", "OPENAI_", "LANGCHAIN_", "LANGSMITH_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("Unexpected network access"))
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://desk.test",
        "https://desk.test/redmine/",
        "http://127.0.0.1:3080/",
        "http://localhost:3080/",
        "http://[::1]:3080/",
    ],
)
def test_service_urls_accept_https_and_loopback_http(url):
    assert validate_url(url) == url.rstrip("/")
    assert RedmineSettings(url=url).url == url.rstrip("/")
    ModelSettings(
        name="test-model", base_url=url, api_key=SecretStr("synthetic-model-key")
    ).require_live()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "desk.test",
        "/relative",
        "ftp://desk.test",
        "http://desk.test",
        "http://127.0.0.1.evil.test",
        "https://user:synthetic-password@desk.test",
        "https://user@desk.test",
        "https://desk.test?api_key=synthetic-query-key",
        "https://desk.test#synthetic-fragment",
    ],
)
def test_service_urls_reject_remote_http_credentials_and_extra_url_fields(url):
    with pytest.raises(ValueError):
        validate_url(url)
    with pytest.raises(ValidationError):
        RedmineSettings(url=url)
    with pytest.raises(ValueError):
        ModelSettings(name="test-model", base_url=url, api_key="synthetic-key").require_live()


@pytest.mark.parametrize(
    ("field", "environment_name"),
    [
        ("name", "DESK_MODEL"),
        ("base_url", "DESK_MODEL_BASE_URL"),
        ("api_key", "DESK_MODEL_API_KEY"),
    ],
)
def test_live_settings_require_each_explicit_field(field, environment_name):
    values = {
        "name": "test-model",
        "base_url": "https://model.test/v1",
        "api_key": "synthetic-model-key",
    }
    values[field] = ""
    with pytest.raises(ValueError, match=environment_name) as raised:
        ModelSettings(**values).require_live()
    assert "synthetic-model-key" not in str(raised.value)


@pytest.mark.parametrize("explicit_key", ["", "synthetic-explicit-key"])
def test_model_key_environment_indirection_and_explicit_precedence(monkeypatch, explicit_key):
    monkeypatch.setenv("DESK_MODEL_API_KEY_ENV", "TEST_SYNTHETIC_PROVIDER_KEY")
    monkeypatch.setenv("TEST_SYNTHETIC_PROVIDER_KEY", "synthetic-indirect-key")
    monkeypatch.setenv("DESK_MODEL_API_KEY", explicit_key)
    settings = ModelSettings.from_env()
    assert settings.api_key.get_secret_value() == (explicit_key or "synthetic-indirect-key")


def test_unset_indirect_key_remains_unconfigured(monkeypatch):
    monkeypatch.setenv("DESK_MODEL", "test-model")
    monkeypatch.setenv("DESK_MODEL_BASE_URL", "https://model.test/v1")
    monkeypatch.setenv("DESK_MODEL_API_KEY_ENV", "TEST_MISSING_PROVIDER_KEY")
    monkeypatch.delenv("TEST_MISSING_PROVIDER_KEY", raising=False)
    settings = ModelSettings.from_env()
    assert settings.api_key.get_secret_value() == ""
    with pytest.raises(ValueError, match="DESK_MODEL_API_KEY"):
        settings.require_live()


@pytest.mark.parametrize("settings_type", [ModelSettings, RedmineSettings])
def test_key_is_redacted_from_settings_serialization(settings_type):
    settings = settings_type(api_key="synthetic-secret-never-print")
    assert isinstance(settings.api_key, SecretStr)
    assert "synthetic-secret-never-print" not in repr(settings)
    assert "synthetic-secret-never-print" not in str(settings)
    assert "synthetic-secret-never-print" not in settings.model_dump_json()
    assert settings.api_key.get_secret_value() == "synthetic-secret-never-print"


def test_environment_settings_parse_numeric_values(monkeypatch):
    values = {
        "DESK_MODEL": "test-model",
        "DESK_MODEL_BASE_URL": "https://model.test/v1",
        "DESK_MODEL_API_KEY": "synthetic-model-key",
        "DESK_MODEL_TIMEOUT": "12.5",
        "DESK_MODEL_MAX_TOKENS": "2048",
        "DESK_INPUT_USD_PER_MILLION": "1.25",
        "DESK_CACHED_INPUT_USD_PER_MILLION": "0.25",
        "DESK_OUTPUT_USD_PER_MILLION": "5",
        "DESK_REDMINE_URL": "https://desk.test/redmine/",
        "DESK_REDMINE_API_KEY": "synthetic-redmine-key",
        "DESK_PROJECT_ID": "7",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    model = ModelSettings.from_env()
    assert (model.timeout, model.max_tokens) == (12.5, 2048)
    assert (model.input_price, model.cached_input_price, model.output_price) == (
        Decimal("1.25"),
        Decimal("0.25"),
        Decimal("5"),
    )
    redmine = RedmineSettings.from_env()
    assert redmine.url == "https://desk.test/redmine"
    assert (redmine.project_id, redmine.timeout) == (7, 15)
    assert redmine.api_key.get_secret_value() == "synthetic-redmine-key"


@pytest.mark.parametrize(
    ("settings_type", "values"),
    [
        (ModelSettings, {"timeout": 0}),
        (ModelSettings, {"max_tokens": 0}),
        (ModelSettings, {"input_price": -1}),
        (ModelSettings, {"cached_input_price": -1}),
        (ModelSettings, {"output_price": -1}),
        (RedmineSettings, {"project_id": 0}),
        (RedmineSettings, {"timeout": -1}),
    ],
)
def test_settings_reject_invalid_numeric_limits(settings_type, values):
    with pytest.raises(ValidationError):
        settings_type(**values)


def test_model_build_uses_explicit_credentials_and_disables_retries(monkeypatch):
    import langchain_openai

    factory = Mock()
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", factory)
    sync_client, async_client = Mock(), Mock()
    monkeypatch.setattr(httpx, "Client", Mock(return_value=sync_client))
    monkeypatch.setattr(httpx, "AsyncClient", Mock(return_value=async_client))
    settings = ModelSettings(
        name="test-model",
        base_url="https://model.test/v1",
        api_key="synthetic-model-key",
        timeout=12,
        max_tokens=512,
    )
    assert settings.build() is factory.return_value
    assert factory.call_args.kwargs == {
        "model": "test-model",
        "base_url": "https://model.test/v1",
        "api_key": settings.api_key,
        "timeout": 12,
        "max_tokens": 512,
        "max_retries": 0,
        "temperature": 0,
        "use_responses_api": False,
        "model_kwargs": {"parallel_tool_calls": False},
        "http_client": sync_client,
        "http_async_client": async_client,
    }
    httpx.Client.assert_called_once_with(timeout=12, trust_env=True)
    httpx.AsyncClient.assert_called_once_with(timeout=12, trust_env=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "trust_env"),
    [
        ("http://127.0.0.1:18317/v1", False),
        ("http://localhost:18317/v1", False),
        ("http://[::1]:18317/v1", False),
        ("https://model.test/v1", True),
    ],
)
async def test_model_transports_are_independent_across_builds(monkeypatch, base_url, trust_env):
    sync_init, async_init = httpx.Client.__init__, httpx.AsyncClient.__init__
    constructed = []
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "id": "mock-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Mock transport is open."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def init_sync(self, *args, **kwargs):
        constructed.append(("sync", dict(kwargs)))
        sync_init(self, *args, **kwargs, transport=httpx.MockTransport(handle))

    def init_async(self, *args, **kwargs):
        constructed.append(("async", dict(kwargs)))
        async_init(self, *args, **kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx.Client, "__init__", init_sync)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init_async)
    settings = ModelSettings(
        name="test-model",
        base_url=base_url,
        api_key="synthetic-model-key",
        timeout=12,
    )
    first = settings.build()
    second = settings.build()
    try:
        assert first.http_client is not second.http_client
        assert first.http_async_client is not second.http_async_client
        assert first.root_client is not second.root_client
        assert first.root_async_client is not second.root_async_client
        assert [kind for kind, _ in constructed] == ["sync", "async", "sync", "async"]
        assert all(options == {"timeout": 12, "trust_env": trust_env} for _, options in constructed)
        first.root_client.close()
        await first.root_async_client.close()
        assert first.http_client.is_closed
        assert first.http_async_client.is_closed
        assert not second.http_client.is_closed
        assert not second.http_async_client.is_closed
        assert second.invoke("Check the mock connection").text == "Mock transport is open."
        assert (await second.ainvoke("Check again")).text == "Mock transport is open."
        assert len(requests) == 2
    finally:
        for model in (first, second):
            model.root_client.close()
            await model.root_async_client.close()


def test_state_directory_is_absolute_private_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_STATE_DIR", "private-state")
    directory = state_dir()
    assert directory == tmp_path / "private-state"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert state_dir() == directory
