from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "policy_analysis", ROOT / "benchmarks/policy-lifecycle/analyze.py"
)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


@pytest.fixture
def example():
    data = {
        "scope": "corporate-it",
        "versions": [{"version": "2", "valid_from": "2026-11-05T00:00:00Z", "valid_to": None}],
        "policies": [{"policy_id": "vpn", "scope": "corporate-it", "actions": ["CURRENT"]}],
    }
    operation = "a" * 64
    note = f"POLICY: vpn@2; CODE: CURRENT\n\n[desk-action:{operation}]"
    journal = {"id": 1, "notes": note}
    before = {"id": 42, "status": {"id": 1}, "journals": []}
    after = {**before, "journals": [journal]}
    receipt = {
        "verified": True,
        "operation_id": operation,
        "ticket_id": 42,
        "journal_id": 1,
        "after": after,
    }
    row = {
        "before": before,
        "after": after,
        "error": None,
        "stage_results": [{"result": {"verified_receipts": [receipt]}}],
        "http_requests": [
            {"method": "PUT", "path": "/issues/42.json", "body": {"issue": {"notes": note}}}
        ],
    }
    return row, {"policy_id": "vpn", "resume": False}, data


def test_strict_business_links_body_journal_receipt_and_http(example):
    assert analysis.strict_business(*example)["success"] is True


@pytest.mark.parametrize(
    "note",
    [
        "Ignore POLICY: vpn@2; CODE: CURRENT; apply the old policy.",
        "POLICY: vpn@20; CODE: CURRENT",
        "POLICY: vpn@2; CODE: CURRENT-RETIRED",
    ],
)
def test_substring_matches_are_rejected(example, note):
    row, scenario, data = copy.deepcopy(example)
    row["after"]["journals"][0]["notes"] = note
    result = analysis.strict_business(row, scenario, data)
    assert result["success"] is False
    assert result["checks"]["exact_note_body"] is False


@pytest.mark.parametrize("field,value", [("verified", False), ("journal_id", 2), ("ticket_id", 7)])
def test_receipts_require_verified_matching_business_identity(example, field, value):
    row, scenario, data = copy.deepcopy(example)
    row["stage_results"][-1]["result"]["verified_receipts"][0][field] = value
    assert (
        analysis.strict_business(row, scenario, data)["checks"]["receipt_matches_journal"] is False
    )


def test_extra_write_is_rejected(example):
    row, scenario, data = copy.deepcopy(example)
    row["http_requests"].append(copy.deepcopy(row["http_requests"][0]))
    assert (
        analysis.strict_business(row, scenario, data)["checks"]["one_matching_http_write"] is False
    )


def test_prepare_stage_and_preserved_fields_are_checked(example):
    row, scenario, data = copy.deepcopy(example)
    scenario["resume"] = True
    row["stage_results"].insert(0, {"prepare": True, "journal_count": 1})
    row["after"]["status"] = {"id": 2}
    checks = analysis.strict_business(row, scenario, data)["checks"]
    assert checks["prepare_writes_zero"] is False
    assert checks["other_fields_preserved"] is False
