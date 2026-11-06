"""Offline regression checks for the frozen business reliability experiment."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import TaskContract
from servicedesk_agent.operations import redmine_store
from servicedesk_agent.redmine import RedmineClient
from servicedesk_agent.session import load_session

SCRIPT = Path(__file__).parents[1] / "scripts/benchmark_business_reliability.py"
SPEC = importlib.util.spec_from_file_location("business_reliability_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_frozen_protocol_has_fifty_paired_losses_and_fifty_normal_operations():
    protocol = benchmark.read_json(benchmark.FIXTURES / "protocol.json")
    assert protocol["main_operations_per_strategy"] == 100
    indices = protocol["loss_operation_indices"]
    assert len(indices) == len(set(indices)) == 50
    assert all(0 <= index < 100 for index in indices)
    assert sum(index % 2 == 0 for index in indices) == 25
    assert protocol["baseline"]["max_sends"] == 2


@pytest.mark.parametrize("strategy", benchmark.STRATEGIES)
@pytest.mark.parametrize("fault", ["none", "response_loss"])
def test_main_pair_uses_durable_commits_and_keeps_raw_accounting(tmp_path, strategy, fault):
    root = tmp_path / strategy
    benchmark.create_case(root, "regression-note", strategy, fault)
    initial = benchmark.run_phase(root, "initial", 0)
    assert len(benchmark.SyntheticRedmine(root).issue()["journals"]) == 1
    if strategy == "at_least_once" and fault == "response_loss":
        assert initial["error"] == "ReadTimeout"
        assert initial["durable_state"]["sends"] == 1
        assert initial["durable_state"]["ack"] is None
    else:
        assert initial["ok"]
    benchmark.run_phase(root, "recover", 1)
    result = benchmark.score_case(root)
    duplicate = int(strategy == "at_least_once" and fault == "response_loss")
    assert result["http_writes"] == result["server_commits"] == 1 + duplicate
    assert result["journal_count"] == 1 + duplicate
    assert result["duplicate_entries"] == duplicate
    assert result["business_complete"] and not result["final_unknown"]
    assert result["exactly_once"] is (duplicate == 0)
    assert result["commit_then_loss_hits"] == int(fault == "response_loss")
    if strategy == "reconcile":
        assert result["verified_receipt_count"] == 1
        assert result["phases"][-1]["result"][0]["replayed"]
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert events == benchmark.SyntheticRedmine(root).events()
    if fault == "response_loss":
        hit = next(event for event in events if event["kind"] == "fault_hit")
        commit = next(event for event in events if event["kind"] == "server_commit")
        assert commit["seq"] < hit["seq"]
        assert hit["committed"] is True


def test_baseline_never_calls_production_write_or_reconciliation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The baseline invoked the production deduplication strategy.")

    monkeypatch.setattr(RedmineClient, "add_note", forbidden)
    monkeypatch.setattr(RedmineClient, "_write", forbidden)
    monkeypatch.setattr(RedmineClient, "reconcile_operations", forbidden)
    root = tmp_path / "baseline"
    benchmark.create_case(root, "raw-retry", "at_least_once", "response_loss")
    benchmark.run_phase(root, "initial", 0)
    benchmark.run_phase(root, "recover", 1)
    assert benchmark.score_case(root)["duplicate_entries"] == 1


def test_baseline_exhausted_retry_preserves_unknown_and_exact_original_body(tmp_path):
    root = tmp_path / "exhaustion"
    spec = benchmark.create_case(root, "retry-limit", "at_least_once", "double_loss")
    for tick, phase in enumerate(("initial", "recover", "observe")):
        benchmark.run_phase(root, phase, tick)
    result = benchmark.score_case(root)
    assert result["http_writes"] == result["server_commits"] == 2
    assert result["final_unknown"] and result["http_ack_count"] == 0
    assert result["phases"][-1]["durable_state"]["sends"] == 2
    assert result["business_complete"] and not result["exactly_once"]
    assert result["unknown_logical_ticks"] == 2
    bodies = [
        event["body"]
        for event in benchmark.SyntheticRedmine(root).events()
        if event["kind"] == "write_admitted"
    ]
    assert bodies == [spec["body"], spec["body"]]


@pytest.mark.parametrize("strategy", benchmark.STRATEGIES)
@pytest.mark.parametrize(
    ("change", "expected"),
    [({"allow_notes": False}, "write_not_authorized"), ({"principal_id": 9}, "identity_changed")],
)
def test_both_strategies_apply_the_same_authorization(tmp_path, strategy, change, expected):
    root = tmp_path / strategy
    spec = benchmark.create_case(root, "authorization", strategy, "response_loss")
    spec["contract"].update(change)
    benchmark.write_json(root / "case.json", spec)
    result = benchmark.run_phase(root, "initial", 0)
    assert result["error"] == expected
    assert not any(
        event["kind"] == "write_admitted" for event in benchmark.SyntheticRedmine(root).events()
    )


@pytest.mark.parametrize("strategy", benchmark.STRATEGIES)
def test_late_commit_remains_visible_across_real_process_recovery(tmp_path, strategy):
    root = tmp_path / strategy
    benchmark.create_case(root, "late-commit", strategy, "late_commit", native=True)
    assert benchmark.child(root, "initial", 0)["exit_code"] == 0
    assert benchmark.SyntheticRedmine(root).issue()["journals"] == []
    assert benchmark.child(root, "recover", 1)["exit_code"] == 0
    if strategy == "reconcile":
        assert benchmark.read_json(root / "recover.json")["unknown"]
        assert benchmark.SyntheticRedmine(root).issue()["journals"] == []
    benchmark.SyntheticRedmine(root, "release", 2).release()
    assert benchmark.child(root, "observe", 3)["exit_code"] == 0
    result = benchmark.score_case(root)
    assert result["business_complete"] and not result["final_unknown"]
    assert result["http_writes"] == (2 if strategy == "at_least_once" else 1)
    assert result["duplicate_entries"] == int(strategy == "at_least_once")
    phases = result["phases"]
    assert len({phase["pid"] for phase in phases}) == 3
    assert all(phase["pid"] != os.getpid() for phase in phases)
    if strategy == "reconcile":
        assert len(phases[-1]["result"]["verified_receipts"]) == 1
        assert phases[-1]["result"]["verified_receipts"][0]["replayed"]
        assert result["unknown_logical_ticks"] == 3


@pytest.mark.parametrize("strategy", benchmark.STRATEGIES)
def test_checkpoint_gap_exits_then_recovers_in_new_process(tmp_path, strategy):
    root = tmp_path / strategy
    spec = benchmark.create_case(root, "checkpoint-gap", strategy, "checkpoint_gap", native=True)
    assert benchmark.child(root, "initial", 0)["exit_code"] == benchmark.GAP_EXIT
    backend = benchmark.SyntheticRedmine(root)
    assert len(backend.issue()["journals"]) == 1
    if strategy == "reconcile":
        contract = TaskContract.model_validate(spec["contract"])
        with (
            redmine_store(root / "runtime") as store,
            RedmineClient(
                RedmineSettings(url=contract.service_url, api_key="synthetic-key"),
                store=store,
                transport=httpx.MockTransport(backend.handle),
            ) as client,
        ):
            assert load_session(root / "runtime", client, contract)["receipts"] == {}
            assert len(client._operations.load(contract.thread_id)["receipts"]) == 1
    assert benchmark.child(root, "recover", 1)["exit_code"] == 0
    result = benchmark.score_case(root)
    assert result["business_complete"] and not result["final_unknown"]
    assert result["duplicate_entries"] == int(strategy == "at_least_once")
    if strategy == "reconcile":
        assert result["http_writes"] == 1
        assert len(result["phases"][-1]["result"]["verified_receipts"]) == 1
        assert all(
            event["method"] == "GET"
            for event in backend.events()
            if event["phase"] == "recover" and event["kind"] == "http_request"
        )


def test_queued_write_revocation_has_explicit_committed_boundary(tmp_path):
    result = benchmark.run_revocation(tmp_path / "revocation")
    assert result["passed"], result["checks"]
    assert result["independent_schedules"] == 1
    assert result["child_processes"] == 4
    assert result["all_http_writes"] == result["post_revoke_http_writes"] == 0
    assert result["post_revoke_http_requests"] > 0
    assert result["revoke_boundary_seq"] is not None
    assert len({phase["pid"] for phase in result["phases"].values()}) == 4


def test_unexpected_failure_is_retained_for_scoring(tmp_path, monkeypatch):
    root = tmp_path / "failure"
    benchmark.create_case(root, "retain-failure", "reconcile", "none")

    def fail(*args, **kwargs):
        raise RuntimeError("Injected regression failure")

    monkeypatch.setattr(RedmineClient, "add_note", fail)
    outcome = benchmark.run_phase(root, "initial", 0)
    assert outcome["unexpected"]
    assert "Injected regression failure" in outcome["traceback"]
    result = benchmark.score_case(root)
    assert not result["business_complete"]
    assert result["unexpected_errors"] == 1
    assert (root / "initial.json").is_file()


def test_cli_output_is_scoped_to_the_authorized_run_prefix():
    with pytest.raises(ValueError, match="results/runs"):
        benchmark.output_path("/tmp/unapproved-output")
