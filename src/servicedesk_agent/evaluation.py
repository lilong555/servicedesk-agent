"""Reproducible development episodes with the upstream user simulator and graders."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import threading
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from servicedesk_agent.benchmark import (
    DeepAgentsCRMAdapter,
    load_fixed_tasks,
    run_episode,
    subset_manifest,
)
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import fingerprint
from servicedesk_agent.provenance import capture_source
from servicedesk_agent.testing import ScriptedChatModel, tool_call
from servicedesk_agent.usage import UsageRecorder, summarize_episodes


class ModelCallLimitError(RuntimeError):
    pass


class ModelCallLimit(BaseCallbackHandler):
    raise_error = True

    def __init__(self, limit: int):
        if limit <= 0:
            raise ValueError("The model call limit must be positive.")
        self.limit = limit
        self.count = 0
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        with self._lock:
            if self.count >= self.limit:
                raise ModelCallLimitError("The configured model request limit has been reached.")
            self.count += 1


def validate_source(source: Path) -> None:
    expected = subset_manifest()["source_commit"]
    actual = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    changes = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual != expected or changes:
        raise ValueError("Use a clean CRM checkout at the frozen source commit.")


def simulator_client(model, recorder: UsageRecorder):
    from harness.llm.base import LLMClient
    from harness.models import ChatResponse, Role, StreamChunk, ToolCall

    class LangChainSimulatorClient(LLMClient):
        async def chat(
            self, messages, tools=None, temperature=0.0, max_tokens=1086, response_format=None
        ):
            converted = []
            for message in messages:
                text = message.content or ""
                if message.role == Role.SYSTEM:
                    converted.append(SystemMessage(content=text))
                elif message.role == Role.USER:
                    converted.append(HumanMessage(content=text))
                elif message.role == Role.ASSISTANT:
                    converted.append(
                        AIMessage(
                            content=text,
                            tool_calls=[
                                {"name": call.name, "args": call.arguments, "id": call.id}
                                for call in message.tool_calls or []
                            ],
                        )
                    )
                else:
                    converted.append(ToolMessage(content=text, tool_call_id=message.tool_call_id))
            target = model.bind_tools([item.model_dump() for item in tools]) if tools else model
            kwargs = {"temperature": temperature, "max_tokens": max_tokens}
            if response_format is not None:
                kwargs["response_format"] = response_format
            result = await target.ainvoke(converted, {"callbacks": [recorder]}, **kwargs)
            usage = result.usage_metadata
            return ChatResponse(
                content=result.text,
                tool_calls=[
                    ToolCall(id=call["id"], name=call["name"], arguments=call["args"])
                    for call in result.tool_calls
                ],
                usage=(
                    {
                        "prompt_tokens": usage["input_tokens"],
                        "completion_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "cache_read_tokens": (usage.get("input_token_details") or {}).get(
                            "cache_read", 0
                        ),
                    }
                    if usage is not None
                    else None
                ),
            )

        async def stream_chat(self, messages, tools=None, temperature=0.0, max_tokens=1086):
            response = await self.chat(messages, tools, temperature, max_tokens)
            yield StreamChunk(
                delta_content=response.content,
                delta_tool_calls=response.tool_calls,
                finish_reason="stop",
                usage=response.usage,
            )

    return LangChainSimulatorClient()


class RecordedUser:
    """Deterministic protocol fixture used exclusively by wiring smoke tests."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        self._cursor = 0

    def reset(self, instruction):
        self._cursor = 0

    async def step(self, response):
        from harness.user_simulator import STOP_SIGNAL

        if self._cursor >= len(self.messages):
            return STOP_SIGNAL
        message = self.messages[self._cursor]
        self._cursor += 1
        return message


def smoke_fixture(task_id: str, *, wrong_status: bool = False):
    if task_id == "itsm_hours_005_add_comment_simple":
        note = "Waiting on procurement to confirm the laptop model."
        return (
            ScriptedChatModel(
                responses=[
                    tool_call(
                        "add_ticket_comment", {"ticket_id": "TCK-1004", "comment": note}, "note"
                    ),
                    "The note is recorded.",
                ]
            ),
            RecordedUser([f"Add this note to TCK-1004: {note}"]),
        )
    if task_id == "itsm_ops_007_update_status_to_in_progress":
        return (
            ScriptedChatModel(
                responses=[
                    tool_call(
                        "update_ticket_status",
                        {
                            "ticket_id": "TCK-1004",
                            "status": "Resolved" if wrong_status else "In Progress",
                        },
                        "status",
                    ),
                    "The status is updated.",
                ]
            ),
            RecordedUser(["Set TCK-1004 to In Progress."]),
        )
    return (
        ScriptedChatModel(
            responses=[
                tool_call(
                    "add_ticket_comment",
                    {"ticket_id": "TCK-1002", "comment": "Asset tag received from procurement."},
                    "note",
                ),
                "The note is recorded.",
                tool_call(
                    "update_ticket_status",
                    {"ticket_id": "TCK-1002", "status": "Resolved"},
                    "status",
                ),
                "TCK-1002 is Resolved.",
            ]
        ),
        RecordedUser(
            [
                "Add a note to TCK-1002: Asset tag received from procurement.",
                "Now set TCK-1002 to Resolved.",
            ]
        ),
    )


async def _components(stack, directory: Path, scope: str, policy: str):
    from langgraph.store.sqlite import AsyncSqliteStore

    from servicedesk_agent.components import build_components
    from servicedesk_agent.knowledge import PolicyCatalog, PolicyVersion

    store = await stack.enter_async_context(
        AsyncSqliteStore.from_conn_string(str(directory / "memory.sqlite"))
    )
    await store.setup()
    documents = [
        PolicyVersion(
            policy_id="itsm-operating-policy",
            version=subset_manifest()["source_commit"],
            source_uri=(
                "https://github.com/salesforce-misc/crm-agent-bench/blob/"
                f"{subset_manifest()['source_commit']}/src/tasks/policies/itsm_policy.md"
            ),
            scope="crm-itsm",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            text=policy,
        )
    ]
    return build_components(scope, store, PolicyCatalog(documents, "crm-itsm"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


async def run_evaluation(
    source: Path,
    output: Path,
    *,
    groups: tuple[str, ...] = ("A", "B"),
    split: str = "calibration",
    live: bool = False,
    settings: ModelSettings | None = None,
    max_calls: int = 300,
    max_turns: int = 15,
) -> dict:
    from harness.tools.domains.itsm import ITSMEnvironment
    from harness.user_simulator import load_user_simulator

    if split not in {"calibration", "development"}:
        raise ValueError("Development runs use calibration or development; holdout stays frozen.")
    if not groups or any(group not in {"A", "B"} for group in groups):
        raise ValueError("Select A and/or B.")
    if not live and split != "calibration":
        raise ValueError("Scripted wiring smoke tests cover the three calibration tasks.")
    settings = settings or ModelSettings.from_env()
    if live:
        settings.require_live()
    validate_source(source)
    tasks = load_fixed_tasks(source, split)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    policy = (source / "src/tasks/policies/itsm_policy.md").read_text()
    gate = ModelCallLimit(max_calls)
    configuration = {
        "model_mode": "live" if live else "scripted",
        "model": settings.model_dump(mode="json", exclude={"api_key"})
        if live
        else "scripted-fixture",
        "split": split,
        "groups": list(groups),
        "tasks": [task.id for task in tasks],
        "source_commit": subset_manifest()["source_commit"],
        "seed": subset_manifest()["seed"],
        "seed_scope": "task ordering and local RNG; provider sampling seed is not configured",
        "max_calls": max_calls,
        "max_turns": max_turns,
        "user_simulator": "react" if live else "recorded-fixture",
        "semantic_judge_calls": 0,
        "native_summarization": True,
        "provenance": capture_source(output),
    }
    _write_json(output / "config.json", configuration)
    episodes = []
    for task in tasks:
        for group in groups:
            if live and gate.count >= gate.limit:
                break
            random.seed(subset_manifest()["seed"])
            directory = output / group / task.id
            directory.mkdir(parents=True, mode=0o700)
            user_usage = UsageRecorder("user_simulator")
            if live:
                agent_model = settings.build().model_copy(update={"callbacks": [gate]})
                user_model = settings.build().model_copy(update={"callbacks": [gate]})
                user = load_user_simulator(
                    strategy="react",
                    llm=simulator_client(user_model, user_usage),
                    temperature=0,
                    max_tokens=1086,
                    max_message_chars=4000,
                )
            else:
                agent_model, user = smoke_fixture(task.id)
            async with AsyncExitStack() as stack:
                if live:
                    for client_model in (agent_model, user_model):
                        stack.callback(client_model.root_client.close)
                        stack.push_async_callback(client_model.root_async_client.close)
                saver = await stack.enter_async_context(
                    AsyncSqliteSaver.from_conn_string(str(directory / "checkpoint.sqlite"))
                )
                scope = fingerprint({"task": task.id, "group": group, "run": str(output.resolve())})
                bundle = (
                    await _components(stack, directory, scope, policy) if group == "B" else None
                )
                adapter = DeepAgentsCRMAdapter(
                    agent_model,
                    ITSMEnvironment(initial_state=task.initial_state),
                    policy,
                    checkpointer=saver,
                    thread_id=scope,
                    components=bundle,
                )
                try:
                    episode = await run_episode(task, adapter, user, max_turns=max_turns)
                except Exception as exc:
                    episode = {
                        "task_id": task.id,
                        "success": None,
                        "error_type": type(exc).__name__,
                        "tool_events": adapter.events,
                        "final_state": adapter.environment.get_state(),
                    }
                agent_summary = adapter.usage.summary(settings if live else None)
                user_summary = user_usage.summary(settings if live else None)
                episode.update(
                    group=group,
                    agent_usage=agent_summary,
                    user_usage=user_summary,
                    agent_cost_usd=agent_summary["cost_usd"],
                    user_cost_usd=user_summary["cost_usd"],
                )
                _write_json(directory / "result.json", episode)
                _write_json(directory / "model_calls.json", adapter.usage.calls + user_usage.calls)
            episodes.append(episode)
            _write_json(output / "episodes.json", episodes)
    negative_control = None
    if not live:
        task = next(
            item for item in tasks if item.id == "itsm_ops_007_update_status_to_in_progress"
        )
        model, user = smoke_fixture(task.id, wrong_status=True)
        async with AsyncSqliteSaver.from_conn_string(str(output / "negative.sqlite")) as saver:
            adapter = DeepAgentsCRMAdapter(
                model,
                ITSMEnvironment(initial_state=task.initial_state),
                policy,
                checkpointer=saver,
                thread_id="negative-control",
            )
            negative_control = await run_episode(task, adapter, user)
        _write_json(output / "negative-control.json", negative_control)
    summary = {
        "model_mode": configuration["model_mode"],
        "external_model_requests": gate.count if live else 0,
        "planned_episodes": len(tasks) * len(groups),
        "groups": {
            group: summarize_episodes([item for item in episodes if item["group"] == group])
            for group in groups
        },
        "negative_control_rejected": (
            negative_control["success"] is False if negative_control is not None else None
        ),
        "results_path": str(output.resolve()),
    }
    if "A" in groups and "B" in groups:
        pairs = {}
        for item in episodes:
            pairs.setdefault(item["task_id"], {})[item["group"]] = item["success"]
        comparable = [
            pair
            for pair in pairs.values()
            if set(pair) == {"A", "B"} and all(value is not None for value in pair.values())
        ]
        summary["B_vs_A"] = {
            "wins": sum(pair["B"] and not pair["A"] for pair in comparable),
            "losses": sum(pair["A"] and not pair["B"] for pair in comparable),
            "ties": sum(pair["A"] == pair["B"] for pair in comparable),
            "unscored_pairs": len(pairs) - len(comparable),
        }
    _write_json(output / "summary.json", summary)
    return summary


def run(*args, **kwargs):
    return asyncio.run(run_evaluation(*args, **kwargs))
