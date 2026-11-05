"""Count native model calls, including summarization and failed requests."""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from servicedesk_agent.config import ModelSettings


class UsageRecorder(BaseCallbackHandler):
    def __init__(self, role: str = "agent") -> None:
        self.role = role
        self.calls: list[dict[str, Any]] = []
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        with self._lock:
            self._started[str(run_id)] = time.monotonic()

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        with self._lock:
            self._started[str(run_id)] = time.monotonic()

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        usage = None
        for group in response.generations:
            for generation in group:
                message = getattr(generation, "message", None)
                if getattr(message, "usage_metadata", None) is not None:
                    usage = dict(message.usage_metadata)
        self.record(str(run_id), usage, error=None)

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        self.record(str(run_id), None, error=type(error).__name__)

    def record(self, run_id: str, usage: dict | None, error: str | None) -> None:
        with self._lock:
            start = self._started.pop(run_id, None)
            self.calls.append(
                {
                    "run_id": run_id,
                    "role": self.role,
                    "elapsed_seconds": time.monotonic() - start if start is not None else None,
                    "usage": usage,
                    "error": error,
                }
            )

    def summary(self, settings: ModelSettings | None = None) -> dict[str, Any]:
        with self._lock:
            calls = list(self.calls)
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        unknown = 0
        for call in calls:
            usage = call["usage"]
            if usage is None:
                unknown += 1
                continue
            totals["input_tokens"] += usage.get("input_tokens", 0)
            totals["output_tokens"] += usage.get("output_tokens", 0)
            totals["cache_read_tokens"] += (usage.get("input_token_details") or {}).get(
                "cache_read", 0
            )
        price = cost_usd(totals, settings) if settings is not None and unknown == 0 else None
        return {
            "role": self.role,
            "calls": len(calls),
            "failed_calls": sum(call["error"] is not None for call in calls),
            "unknown_usage_calls": unknown,
            **totals,
            "cost_usd": price,
        }


def cost_usd(usage: dict[str, int], settings: ModelSettings) -> float | None:
    if settings.input_price is None or settings.output_price is None:
        return None
    cached = usage.get("cache_read_tokens", 0)
    input_tokens = usage.get("input_tokens", 0)
    if cached < 0 or input_tokens < cached:
        return None
    if cached and settings.cached_input_price is None:
        return None
    price = (
        Decimal(input_tokens - cached) * settings.input_price
        + Decimal(cached) * (settings.cached_input_price or Decimal(0))
        + Decimal(usage.get("output_tokens", 0)) * settings.output_price
    ) / Decimal(1_000_000)
    return float(price)


def summarize_episodes(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    started = len(episodes)
    successes = sum(item.get("success") is True for item in episodes)
    scored = sum(item.get("success") is not None for item in episodes)
    service_costs = [item.get("agent_cost_usd") for item in episodes]
    user_costs = [item.get("user_cost_usd") for item in episodes]
    service_cost = sum(service_costs) if all(value is not None for value in service_costs) else None
    user_cost = sum(user_costs) if all(value is not None for value in user_costs) else None
    return {
        "started": started,
        "scored": scored,
        "successes": successes,
        "failures": scored - successes,
        "infrastructure_errors": started - scored,
        "success_per_started": successes / started if started else None,
        "success_per_scored": successes / scored if scored else None,
        "agent_cost_usd": service_cost,
        "user_cost_usd": user_cost,
        "experiment_cost_usd": (
            service_cost + user_cost if service_cost is not None and user_cost is not None else None
        ),
        "cost_per_success_usd": service_cost / successes
        if successes and service_cost is not None
        else None,
    }
