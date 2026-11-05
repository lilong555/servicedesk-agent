"""Explicit scripted model fixtures; these measure wiring rather than model quality."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class ScriptedChatModel(BaseChatModel):
    model_name: str = "openai:desk-scripted"
    responses: list[Any] = Field(default_factory=list, exclude=True)
    _cursor: int = PrivateAttr(default=0)
    _requests: list[list[Any]] = PrivateAttr(default_factory=list)
    _visible_tools: list[list[str]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "desk-scripted"

    @property
    def requests(self) -> list[list[Any]]:
        return self._requests

    @property
    def visible_tools(self) -> list[list[str]]:
        return self._visible_tools

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self._visible_tools.append(
            [item.name if hasattr(item, "name") else item["function"]["name"] for item in tools]
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._requests.append(messages)
        if self._cursor >= len(self.responses):
            raise ValueError("Scripted model fixture exhausted.")
        response = self.responses[self._cursor]
        self._cursor += 1
        if callable(response):
            response = response(messages)
        if isinstance(response, str):
            response = AIMessage(content=response)
        return ChatResult(generations=[ChatGeneration(message=response.model_copy(deep=True))])


def tool_call(name: str, arguments: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id}])
