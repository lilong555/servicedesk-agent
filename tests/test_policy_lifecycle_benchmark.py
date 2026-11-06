from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "policy_benchmark", ROOT / "scripts/benchmark_policy_lifecycle.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


@pytest.fixture
def data():
    return bench.read_json(bench.CASES)


def test_dataset_has_adjacent_versions_and_outside_scope_controls(data):
    docs = bench.documents(data)
    assert len(docs) == 24
    assert len(data["queries"]) == 12
    catalog = bench.ExperimentalCatalog(
        docs, data["scope"], filter_time=True, clock=bench.at("2026-11-06T00:00:00Z")
    )
    catalog._load()
    assert len(catalog._documents) == 24
    assert {doc.scope for doc in docs} == {"corporate-it", "laboratory-it"}


def test_only_temporal_search_filter_changes(data):
    catalogs = [
        bench.ExperimentalCatalog(
            bench.documents(data),
            data["scope"],
            filter_time=mode,
            clock=bench.at("2026-11-06T00:00:00Z"),
        )
        for mode in (False, True)
    ]
    for catalog in catalogs:
        found = catalog.search("VPN remote access maintenance")
        assert all(item["evidence"]["entity_scope"] == data["scope"] for item in found)
    assert catalogs[0].read("vpn-maintenance") == catalogs[1].read("vpn-maintenance")
    assert catalogs[0].read("vpn-maintenance", "1") == catalogs[1].read("vpn-maintenance", "1")
    assert catalogs[1].read("vpn-maintenance", "1")["status"] == "historical"


def test_explicit_clock_and_no_eligible_results(data):
    catalog = bench.ExperimentalCatalog(
        bench.documents(data),
        data["scope"],
        filter_time=True,
        clock=bench.at("2026-11-04T00:00:00Z"),
    )
    assert catalog.search("VPN remote access maintenance")[0]["version"] == "1"
    catalog.clock = bench.at("2026-11-06T00:00:00Z")
    assert catalog.search("VPN remote access maintenance")[0]["version"] == "2"
    assert catalog.search("VPN", now=bench.at("2024-01-01T00:00:00Z")) == []


def test_historical_gold_is_correct_and_unanswerable_is_separate(data):
    result = bench.retrieval_experiment(data)
    for arm in bench.ARMS:
        assert result["summary"][arm]["answerable_queries"] == 9
        assert result["summary"][arm]["queries"] == 12
        assert result["summary"][arm]["empty_eligible_cases"] == 2
    historical = next(
        row
        for row in result["rows"]
        if row["query"]["id"] == "vpn-history" and row["arm"] == "filtered_time"
    )
    assert historical["metrics"]["hit_at_1"] is True
    unanswerable = next(row for row in result["rows"] if row["query"]["id"] == "unrelated-query")
    assert unanswerable["metrics"]["hit_at_1"] is None


def test_model_task_excludes_gold_action_and_version(data):
    for scenario in data["agent_scenarios"]:
        message = bench.task_message(scenario)
        assert f"{scenario['policy_id']}@2" not in message
        for policy in data["policies"]:
            assert all(action not in message for action in policy["actions"])


def test_business_grader_rejects_stale_policy_and_unverified_notes(data):
    backend = bench.TicketBackend()
    scenario = data["agent_scenarios"][0]
    backend.issue["journals"] = [{"notes": "POLICY: vpn-maintenance@1; CODE: NET-MON-2300"}]
    stages = [{"result": {"verified_receipts": [{}]}, "journal_count": 1}]
    result = bench.score_business(scenario, data, backend, stages)
    assert result["success"] is False
    assert result["checks"]["correct_policy_version"] is False
    backend.issue["journals"][0]["notes"] = "POLICY: vpn-maintenance@2; CODE: IDENTITY-WED-0100"
    assert bench.score_business(scenario, data, backend, stages)["success"] is True
    stages[0]["result"]["verified_receipts"] = []
    assert bench.score_business(scenario, data, backend, stages)["success"] is False
