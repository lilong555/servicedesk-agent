"""One fixed native harness configuration shared by the application and evaluations."""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel


def create_native_agent(model: BaseChatModel, **kwargs: Any):
    name = getattr(model, "model_name", None)
    if not name:
        raise ValueError("Provide a named ChatOpenAI model or the explicit scripted test model.")
    key = name if name.startswith("openai:") else f"openai:{name}"
    register_harness_profile(
        key,
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=frozenset({"execute"}),
        ),
    )
    return create_deep_agent(model=model, subagents=[], **kwargs)
