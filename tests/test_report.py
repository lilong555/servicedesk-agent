from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def report_runs(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/report.py"
    spec = importlib.util.spec_from_file_location("report_script", path)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    usage = {
        "calls": 2,
        "failed_calls": 0,
        "unknown_usage_calls": 0,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 40,
    }
    runs = []
    for group, success in (("A", True), ("B", False)):
        directory = tmp_path / group
        directory.mkdir()
        configuration = {
            "model_mode": "live",
            "model": {"name": "test-model"},
            "split": "development",
            "tasks": ["task-1"],
            "source_commit": "fixed-source",
            "provenance": {"implementation_sha256": "fixed-implementation"},
        }
        (directory / "config.json").write_text(json.dumps(configuration))
        (directory / "episodes.json").write_text(
            json.dumps(
                [
                    {
                        "task_id": "task-1",
                        "group": group,
                        "success": success,
                        "agent_usage": usage,
                        "user_usage": usage,
                        "agent_cost_usd": None,
                        "user_cost_usd": None,
                        "trace": {"elapsed_seconds": 3, "tool_calls_detail": [{"name": "tool"}]},
                    }
                ]
            )
        )
        runs.append(directory)
    return report, runs


def test_paired_report_preserves_metrics_and_failures(report_runs):
    report, runs = report_runs
    result = report.combine(runs)
    assert result["B_vs_A"] == {"wins": 0, "losses": 1, "ties": 0, "unscored_pairs": 0}
    assert result["groups"]["B"]["started"] == result["groups"]["B"]["failures"] == 1
    assert result["groups"]["B"]["cost_per_success_usd"] is None
    assert result["groups"]["A"]["agent_usage"]["input_tokens"] == 100
    assert result["groups"]["A"]["elapsed_seconds"] == 3
    assert result["groups"]["A"]["tool_calls"] == 1
    assert len(result["inputs"]) == 2
    assert all(len(item["episodes_sha256"]) == 64 for item in result["inputs"])


@pytest.mark.parametrize("change", ["model", "implementation", "duplicate"])
def test_report_rejects_incomparable_or_duplicate_runs(report_runs, change):
    report, runs = report_runs
    configuration = json.loads((runs[1] / "config.json").read_text())
    if change == "model":
        configuration["model"]["name"] = "other-model"
    elif change == "implementation":
        configuration["provenance"]["implementation_sha256"] = "other-implementation"
    else:
        runs = [runs[0], runs[0]]
    if change != "duplicate":
        (runs[1] / "config.json").write_text(json.dumps(configuration))
    with pytest.raises(ValueError):
        report.combine(runs)
