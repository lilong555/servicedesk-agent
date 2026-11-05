"""Offline evaluation wiring against the pinned CRM runner and official graders."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field

from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import fingerprint
from servicedesk_agent.evaluation import ModelCallLimit, ModelCallLimitError, run_evaluation
from servicedesk_agent.testing import ScriptedChatModel, tool_call

SOURCE = Path(__file__).resolve().parents[1] / "vendor/crm-agent-bench"
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(not SOURCE.exists(), reason="Fetch the pinned CRM source first."),
]
CALIBRATION = [
    "itsm_hours_005_add_comment_simple",
    "itsm_ops_007_update_status_to_in_progress",
    "itsm_ops_012_comment_then_status_update_order",
]


@pytest.fixture(autouse=True)
def offline_model_factory(monkeypatch):
    def reject_live_model(_settings):
        pytest.fail("This regression requires an explicit ScriptedChatModel factory.")

    monkeypatch.setattr(ModelSettings, "build", reject_live_model)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ManagedScriptedChatModel(ScriptedChatModel):
    root_client: Any = Field(default_factory=lambda: SimpleNamespace(close=Mock()), exclude=True)
    root_async_client: Any = Field(
        default_factory=lambda: SimpleNamespace(close=AsyncMock()), exclude=True
    )


@pytest.mark.asyncio
async def test_scripted_evaluation_full_ab_smoke_and_negative_control(tmp_path):
    from harness.tools.domains.itsm import ITSMEnvironment

    from servicedesk_agent.benchmark import DeepAgentsCRMAdapter, subset_manifest

    output = tmp_path / "evaluation"
    summary = await run_evaluation(SOURCE, output, settings=ModelSettings())
    configuration = read_json(output / "config.json")
    episodes = read_json(output / "episodes.json")
    assert read_json(output / "summary.json") == summary
    assert configuration["model_mode"] == summary["model_mode"] == "scripted"
    assert configuration["model"] == "scripted-fixture"
    assert configuration["user_simulator"] == "recorded-fixture"
    assert configuration["semantic_judge_calls"] == 0
    assert configuration["native_summarization"] is True
    assert configuration["source_commit"] == subset_manifest()["source_commit"]
    assert configuration["seed"] == subset_manifest()["seed"]
    assert configuration["split"] == "calibration"
    assert configuration["groups"] == ["A", "B"]
    assert configuration["tasks"] == CALIBRATION
    assert summary["external_model_requests"] == 0
    assert summary["planned_episodes"] == len(episodes) == 6
    assert summary["negative_control_rejected"] is True
    assert summary["results_path"] == str(output.resolve())
    assert summary["B_vs_A"] == {"wins": 0, "losses": 0, "ties": 3, "unscored_pairs": 0}
    assert [(item["task_id"], item["group"]) for item in episodes] == [
        (task_id, group) for task_id in CALIBRATION for group in ("A", "B")
    ]
    for group in ("A", "B"):
        totals = summary["groups"][group]
        assert totals["started"] == totals["scored"] == totals["successes"] == 3
        assert totals["failures"] == totals["infrastructure_errors"] == 0
        assert totals["success_per_started"] == totals["success_per_scored"] == 1
        assert totals["experiment_cost_usd"] is None

    for episode in episodes:
        directory = output / episode["group"] / episode["task_id"]
        assert read_json(directory / "result.json") == episode
        assert episode["success"] is True
        assert episode["trace"]["error"] is None
        assert episode["trace"]["args_passed"] is True
        assert episode["trace"]["state_passed"] is True
        assert all(
            event["ok"] and event["received_result"]
            for event in episode["trace"]["tool_calls_detail"]
        )
        calls = read_json(directory / "model_calls.json")
        expected_calls = 4 if episode["task_id"] == CALIBRATION[2] else 2
        assert len(calls) == episode["agent_usage"]["calls"] == expected_calls
        assert all(call["role"] == "agent" and call["error"] is None for call in calls)
        assert episode["agent_usage"]["unknown_usage_calls"] == expected_calls
        assert episode["agent_cost_usd"] is episode["user_cost_usd"] is None
        assert episode["user_usage"]["calls"] == 0
        scope = fingerprint(
            {"task": episode["task_id"], "group": episode["group"], "run": str(output.resolve())}
        )
        async with AsyncSqliteSaver.from_conn_string(str(directory / "checkpoint.sqlite")) as saver:
            adapter = DeepAgentsCRMAdapter(
                ScriptedChatModel(),
                ITSMEnvironment(initial_state=episode["initial_state"]),
                "Read the persisted checkpoint.",
                checkpointer=saver,
                thread_id=scope,
            )
            snapshot = await adapter.graph.aget_state(adapter.config)
        assert snapshot.next == ()
        state = snapshot.values
        assert any(message.type == "tool" for message in state["messages"])
        if episode["group"] == "B":
            assert (directory / "memory.sqlite").is_file()
            assert "/skills/resume-ticket/SKILL.md" in state["files"]
            assert "/skills/ticket-handling/SKILL.md" in state["files"]
        else:
            assert not (directory / "memory.sqlite").exists()

    for first, second in zip(episodes[::2], episodes[1::2], strict=True):
        assert first["initial_state"] == second["initial_state"]
        assert first["final_state"] == second["final_state"]
    ordered = next(item for item in episodes if item["task_id"] == CALIBRATION[2])
    assert ordered["trace"]["order_passed"] is True
    assert ordered["trace"]["num_turns"] == 2
    assert [event["name"] for event in ordered["trace"]["tool_calls_detail"]] == [
        "add_ticket_comment",
        "update_ticket_status",
    ]
    negative = read_json(output / "negative-control.json")
    assert negative["task_id"] == CALIBRATION[1]
    assert negative["success"] is False
    assert negative["trace"]["error"] is None
    assert negative["trace"]["args_passed"] is False
    assert negative["trace"]["state_passed"] is False
    assert negative["trace"]["tool_calls_detail"][0]["ok"] is True
    assert negative["final_state"]["tickets"]["TCK-1004"]["status"] == "Resolved"
    assert (output / "negative.sqlite").is_file()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.rglob("*.json"))


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("split", ["holdout", "all", "unknown"])
@pytest.mark.asyncio
async def test_evaluation_rejects_non_development_splits_before_model_build(tmp_path, live, split):
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="holdout stays frozen"):
        await run_evaluation(SOURCE, output, split=split, live=live, settings=ModelSettings())
    assert not output.exists()


@pytest.mark.asyncio
async def test_scripted_evaluation_rejects_development_split(tmp_path):
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="three calibration tasks"):
        await run_evaluation(SOURCE, output, split="development", settings=ModelSettings())
    assert not output.exists()


@pytest.mark.parametrize("groups", [(), ("C",), ("A", "C")])
@pytest.mark.asyncio
async def test_evaluation_rejects_invalid_groups(tmp_path, groups):
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="Select A and/or B"):
        await run_evaluation(SOURCE, output, groups=groups, settings=ModelSettings())
    assert not output.exists()


@pytest.mark.parametrize("split", ["calibration", "development"])
@pytest.mark.parametrize("max_calls", [1, 2, 3])
@pytest.mark.asyncio
async def test_model_call_limit_is_persisted_with_scripted_live_orchestration(
    tmp_path, monkeypatch, split, max_calls
):
    from servicedesk_agent.benchmark import load_fixed_tasks

    models = [
        ManagedScriptedChatModel(
            responses=[
                tool_call(
                    "add_ticket_comment",
                    {"ticket_id": "TCK-1004", "comment": "Waiting on procurement."},
                    "comment",
                ),
                "The note is recorded.",
            ]
        ),
        ManagedScriptedChatModel(
            responses=[
                json.dumps(
                    {
                        "stop": False,
                        "message": "Add a procurement note to TCK-1004.",
                        "exit_conditions_met": False,
                    }
                ),
                json.dumps({"stop": True, "message": "", "exit_conditions_met": True}),
            ]
        ),
    ]
    available_models = iter(models)
    monkeypatch.setattr(ModelSettings, "build", lambda _settings: next(available_models))
    # The live orchestration and upstream React simulator use only these scripted models.
    settings = ModelSettings(
        name="offline-limit-fixture", base_url="https://model.test/v1", api_key="test-only"
    )
    output = tmp_path / "limited"
    summary = await run_evaluation(
        SOURCE, output, split=split, live=True, settings=settings, max_calls=max_calls
    )
    episodes = read_json(output / "episodes.json")
    configuration = read_json(output / "config.json")
    assert configuration["split"] == split
    assert configuration["tasks"] == [task.id for task in load_fixed_tasks(SOURCE, split)]
    assert "api_key" not in configuration["model"]
    assert configuration["max_calls"] == max_calls
    assert summary["planned_episodes"] == (6 if split == "calibration" else 18)
    assert summary["external_model_requests"] == max_calls
    assert sum(len(model.requests) for model in models) == max_calls
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["group"] == "A"
    assert episode["success"] is None
    if max_calls == 3:
        assert episode["error_type"] == "ModelCallLimitError"
        events = episode["tool_events"]
    else:
        assert "ModelCallLimitError" in episode["trace"]["error"]
        events = episode["trace"]["tool_calls_detail"]
    assert len(events) == (0 if max_calls == 1 else 1)
    assert all(event["ok"] and event["received_result"] for event in events)
    assert episode["final_state"]["tickets"]["TCK-1004"]["comments"] == (
        [] if max_calls == 1 else ["Waiting on procurement."]
    )
    assert summary["groups"]["A"]["infrastructure_errors"] == 1
    assert summary["groups"]["A"]["scored"] == 0
    assert summary["groups"]["A"]["successes"] == 0
    assert summary["groups"]["B"]["started"] == 0
    assert summary["B_vs_A"] == {"wins": 0, "losses": 0, "ties": 0, "unscored_pairs": 1}
    assert summary["negative_control_rejected"] is None
    assert not (output / "negative-control.json").exists()
    directory = output / "A" / episode["task_id"]
    assert read_json(directory / "result.json") == episode
    assert read_json(output / "summary.json") == summary
    calls = read_json(directory / "model_calls.json")
    assert sum(call["error"] is None for call in calls) == max_calls
    assert any(call["role"] == "user_simulator" for call in calls)
    for model in models:
        model.root_client.close.assert_called_once_with()
        model.root_async_client.close.assert_awaited_once_with()


@pytest.mark.parametrize("provider_failure", [False, True], ids=["success", "provider-error"])
@pytest.mark.asyncio
async def test_live_evaluation_closes_isolated_clients_between_episodes(
    tmp_path, monkeypatch, provider_failure
):
    from langchain_openai import ChatOpenAI

    from servicedesk_agent.evaluation import smoke_fixture

    models = []
    requests = []
    settings = ModelSettings(
        name="offline-transport-fixture", base_url="https://model.test/v1", api_key="test-only"
    )

    def build(_settings):
        index = len(models)
        if index and index % 2 == 0:
            assert all(model.root_client.is_closed() for model in models)
            assert all(model.root_async_client.is_closed() for model in models)
        scripted, user = smoke_fixture(CALIBRATION[index // 4])
        responses = (
            scripted.responses
            if index % 2 == 0
            else [
                json.dumps({"stop": False, "message": text, "exit_conditions_met": False})
                for text in user.messages
            ]
            + [json.dumps({"stop": True, "message": "", "exit_conditions_met": True})]
        )
        remaining = iter(responses)

        def handle(request):
            assert request.method == "POST"
            assert request.url.host == "model.test"
            assert request.url.path == "/v1/chat/completions"
            payload = json.loads(request.content)
            assert payload["model"] == settings.name
            assert payload["messages"]
            requests.append(index)
            if provider_failure and index == 0:
                return httpx.Response(503, json={"error": {"message": "Offline service error."}})
            scripted_response = next(remaining)
            if isinstance(scripted_response, str):
                scripted_response = AIMessage(content=scripted_response)
            message = {"role": "assistant", "content": scripted_response.text}
            if scripted_response.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["args"]),
                        },
                    }
                    for call in scripted_response.tool_calls
                ]
            return httpx.Response(
                200,
                json={
                    "id": f"offline-{index}-{len(requests)}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": settings.name,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls"
                            if scripted_response.tool_calls
                            else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )

        model = ChatOpenAI(
            model=settings.name,
            base_url=settings.base_url,
            api_key=settings.api_key,
            max_retries=0,
            use_responses_api=False,
            temperature=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle), trust_env=False),
            http_async_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        )
        model.root_client.close = Mock(wraps=model.root_client.close)
        model.root_async_client.close = AsyncMock(wraps=model.root_async_client.close)
        models.append(model)
        return model

    monkeypatch.setattr(ModelSettings, "build", build)
    output = tmp_path / "transport"
    try:
        summary = await run_evaluation(SOURCE, output, live=True, settings=settings)
        episodes = read_json(output / "episodes.json")
        assert len(models) == 12
        assert len(episodes) == summary["planned_episodes"] == 6
        assert (
            summary["external_model_requests"] == len(requests) == (28 if provider_failure else 30)
        )
        assert len({id(model.root_client) for model in models}) == len(models)
        assert len({id(model.root_async_client) for model in models}) == len(models)
        assert len({id(model.http_client) for model in models}) == len(models)
        assert len({id(model.http_async_client) for model in models}) == len(models)
        assert all(model.root_client.is_closed() for model in models)
        assert all(model.root_async_client.is_closed() for model in models)
        assert all(model.http_client.is_closed for model in models)
        assert all(model.http_async_client.is_closed for model in models)
        for model in models:
            model.root_client.close.assert_called_once_with()
            model.root_async_client.close.assert_awaited_once_with()
        assert summary["groups"]["B"]["successes"] == 3
        assert summary["groups"]["A"]["successes"] == (2 if provider_failure else 3)
        assert summary["groups"]["A"]["infrastructure_errors"] == int(provider_failure)
        assert summary["B_vs_A"]["unscored_pairs"] == int(provider_failure)
        assert all(episode["success"] is True for episode in episodes[1:])
        if provider_failure:
            assert episodes[0]["success"] is None
            assert episodes[0]["trace"]["error"].startswith("OpenAIAPIError:")
            assert "503" in episodes[0]["trace"]["error"]
            assert episodes[0]["agent_usage"]["failed_calls"] == 1
            assert requests.count(0) == 1
        else:
            assert episodes[0]["success"] is True
        assert read_json(output / "summary.json") == summary
    finally:
        for model in models:
            if not model.root_client.is_closed():
                model.root_client.close()
            if not model.root_async_client.is_closed():
                await model.root_async_client.close()


@pytest.mark.parametrize("limit", [0, -1])
def test_model_call_limit_requires_positive_budget(limit):
    with pytest.raises(ValueError, match="positive"):
        ModelCallLimit(limit)


def test_model_call_limit_admits_exact_budget_under_concurrency():
    gate = ModelCallLimit(5)

    def attempt(_index):
        try:
            gate.on_chat_model_start({}, [[]])
        except ModelCallLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(attempt, range(32)))
    assert sum(admitted) == gate.count == 5


class InspectingScriptedChatModel(ScriptedChatModel):
    call_options: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    bound_definitions: list[list[dict[str, Any]]] = Field(default_factory=list, exclude=True)

    def bind_tools(self, tools, **kwargs):
        self.bound_definitions.append(tools)
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_options.append(kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("cache_read", [None, 0, 40])
@pytest.mark.asyncio
async def test_simulator_client_maps_official_messages_tools_and_usage(cache_read):
    from harness.llm.base import LLMClient
    from harness.models import (
        ChatMessage,
        ChatResponse,
        Role,
        ToolCall,
        ToolDefinition,
        ToolFunction,
    )

    from servicedesk_agent.evaluation import simulator_client
    from servicedesk_agent.usage import UsageRecorder

    usage = (
        None
        if cache_read is None
        else {
            "input_tokens": 120,
            "output_tokens": 7,
            "total_tokens": 127,
            "input_token_details": {"cache_read": cache_read},
        }
    )
    response = AIMessage(
        content="Inspect the dependency.",
        tool_calls=[{"name": "get_ticket", "args": {"ticket_id": "TCK-1002"}, "id": "next"}],
        usage_metadata=usage,
    )
    model = InspectingScriptedChatModel(responses=[response])
    recorder = UsageRecorder("user_simulator")
    client = simulator_client(model, recorder)
    assert isinstance(client, LLMClient)
    messages = [
        ChatMessage(role=Role.SYSTEM, content="Follow the user protocol."),
        ChatMessage(role=Role.USER, content="Read the ticket."),
        ChatMessage(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=[
                ToolCall(id="previous", name="get_ticket", arguments={"ticket_id": "TCK-1001"})
            ],
        ),
        ChatMessage(role=Role.TOOL, content='{"status":"New"}', tool_call_id="previous"),
    ]
    definition = ToolDefinition(
        function=ToolFunction(
            name="get_ticket",
            description="Read a ticket.",
            parameters={
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
            },
        )
    )
    response_format = {"type": "json_object"}
    result = await client.chat(
        messages,
        tools=[definition],
        temperature=0.25,
        max_tokens=321,
        response_format=response_format,
    )
    assert isinstance(result, ChatResponse)
    assert [type(message) for message in model.requests[0]] == [
        SystemMessage,
        HumanMessage,
        AIMessage,
        ToolMessage,
    ]
    assert [message.content for message in model.requests[0]] == [
        "Follow the user protocol.",
        "Read the ticket.",
        "",
        '{"status":"New"}',
    ]
    assert model.requests[0][2].tool_calls[0] == {
        "name": "get_ticket",
        "args": {"ticket_id": "TCK-1001"},
        "id": "previous",
        "type": "tool_call",
    }
    assert model.requests[0][3].tool_call_id == "previous"
    assert model.bound_definitions == [[definition.model_dump()]]
    assert model.call_options == [
        {"temperature": 0.25, "max_tokens": 321, "response_format": response_format}
    ]
    assert result.content == response.text
    assert result.tool_calls == [
        ToolCall(id="next", name="get_ticket", arguments={"ticket_id": "TCK-1002"})
    ]
    assert result.usage == (
        None
        if usage is None
        else {
            "prompt_tokens": 120,
            "completion_tokens": 7,
            "total_tokens": 127,
            "cache_read_tokens": cache_read,
        }
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["role"] == "user_simulator"
    assert recorder.calls[0]["usage"] == usage
    assert recorder.calls[0]["error"] is None
    assert recorder.summary()["unknown_usage_calls"] == int(usage is None)


@pytest.mark.asyncio
async def test_simulator_client_streams_official_chunk_without_tools():
    from harness.models import ChatMessage, Role, StreamChunk

    from servicedesk_agent.evaluation import simulator_client
    from servicedesk_agent.usage import UsageRecorder

    model = InspectingScriptedChatModel(responses=["Continue investigating."])
    recorder = UsageRecorder("user_simulator")
    async with simulator_client(model, recorder) as client:
        chunks = [
            chunk
            async for chunk in client.stream_chat(
                [ChatMessage(role=Role.USER, content=None)], temperature=0.1, max_tokens=64
            )
        ]
    assert len(chunks) == 1
    assert isinstance(chunks[0], StreamChunk)
    assert chunks[0].delta_content == "Continue investigating."
    assert chunks[0].delta_tool_calls == []
    assert chunks[0].finish_reason == "stop"
    assert chunks[0].usage is None
    assert model.requests[0][0].content == ""
    assert model.bound_definitions == []
    assert model.call_options == [{"temperature": 0.1, "max_tokens": 64}]
    assert recorder.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_simulator_model_failure_is_recorded_and_propagated():
    from harness.models import ChatMessage, Role

    from servicedesk_agent.evaluation import simulator_client
    from servicedesk_agent.usage import UsageRecorder

    def fail(_messages):
        raise ModelCallLimitError("Scripted model failure.")

    recorder = UsageRecorder("user_simulator")
    client = simulator_client(ScriptedChatModel(responses=[fail]), recorder)
    with pytest.raises(ModelCallLimitError, match="Scripted model failure"):
        await client.chat([ChatMessage(role=Role.USER, content="Continue.")])
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["error"] == "ModelCallLimitError"
    assert recorder.calls[0]["usage"] is None
    assert recorder.summary()["failed_calls"] == 1
