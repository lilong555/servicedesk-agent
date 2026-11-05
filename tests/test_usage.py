from decimal import Decimal

from servicedesk_agent.config import ModelSettings
from servicedesk_agent.usage import UsageRecorder, cost_usd, summarize_episodes


def test_unknown_usage_and_prices_remain_unknown():
    meter = UsageRecorder()
    meter.record("failed", None, "Timeout")
    assert meter.summary()["unknown_usage_calls"] == 1
    assert meter.summary()["cost_usd"] is None
    assert cost_usd({"input_tokens": 100}, ModelSettings()) is None


def test_cached_tokens_are_counted_inside_logical_input():
    settings = ModelSettings(
        input_price=Decimal("1"), cached_input_price=Decimal("0.1"), output_price=Decimal("5")
    )
    usage = {"input_tokens": 1000, "cache_read_tokens": 500, "output_tokens": 100}
    assert cost_usd(usage, settings) == 0.00105
    assert (
        cost_usd(usage, ModelSettings(input_price=Decimal("1"), output_price=Decimal("5"))) is None
    )


def test_all_attempts_remain_in_denominator_and_cost_numerator():
    summary = summarize_episodes(
        [
            {"success": True, "agent_cost_usd": 1, "user_cost_usd": 0.2},
            {"success": False, "agent_cost_usd": 2, "user_cost_usd": 0.3},
            {"success": None, "agent_cost_usd": 3, "user_cost_usd": 0.4},
        ]
    )
    assert summary["started"] == 3
    assert summary["scored"] == 2
    assert summary["success_per_started"] == 1 / 3
    assert summary["cost_per_success_usd"] == 6
    assert summary["experiment_cost_usd"] == 6.9


def test_zero_successes_and_missing_prices():
    summary = summarize_episodes([{"success": False, "agent_cost_usd": 1, "user_cost_usd": None}])
    assert summary["cost_per_success_usd"] is None
    assert summary["experiment_cost_usd"] is None
