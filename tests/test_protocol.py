from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from servicedesk_agent import evaluation, protocol
from servicedesk_agent.benchmark import POLICY_PROFILE, subset_manifest
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import fingerprint
from servicedesk_agent.provenance import source_manifest

SOURCE = Path(__file__).resolve().parents[1] / "vendor/crm-agent-bench"
pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(not SOURCE.exists(), reason="Fetch the pinned CRM source first."),
]


@pytest.fixture
def settings():
    return ModelSettings(
        name="offline-freeze-fixture",
        base_url="https://model.test/v1",
        api_key="synthetic-private-credential",
    )


@pytest.fixture
def frozen(tmp_path, settings):
    directory = tmp_path / "protocol"
    record = protocol.freeze_evaluation(SOURCE, directory, settings=settings)
    return directory / "freeze.json", record


def validate(path, settings, **overrides):
    options = {"settings": settings, "groups": ("A", "B"), "max_calls": 300, "max_turns": 15}
    options.update(overrides)
    return protocol.validate_freeze(path, SOURCE, **options)


def read_json(path):
    return json.loads(path.read_text())


def test_freeze_records_sources_parameters_and_tasks_without_secrets(frozen, settings):
    path, record = frozen
    assert record["freeze_id"] == fingerprint(record["protocol"])
    assert record["protocol"]["tasks"] == subset_manifest()["holdout"]
    assert record["protocol"]["policy_profile"] == POLICY_PROFILE
    assert record["protocol"]["groups"] == ["A", "B"]
    assert record["protocol"]["packages"] == source_manifest()["packages"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-private-credential" not in path.read_text()
    assert "api_key" not in record["protocol"]["model"]
    assert validate(path, settings) == record
    rotated = settings.model_copy(update={"api_key": ModelSettings(api_key="rotated").api_key})
    assert validate(path, rotated) == record
    with pytest.raises(FileExistsError):
        protocol.freeze_evaluation(SOURCE, path.parent, settings=settings)


@pytest.mark.parametrize(
    "options",
    [
        {"groups": ()},
        {"groups": ("C",)},
        {"groups": ("A", "A")},
        {"max_calls": 0},
        {"max_calls": -1},
        {"max_calls": True},
        {"max_turns": 0},
        {"max_turns": -1},
        {"max_turns": False},
    ],
)
def test_freeze_rejects_invalid_parameters_before_output(tmp_path, settings, options):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        protocol.freeze_evaluation(SOURCE, output, settings=settings, **options)
    assert not output.exists()


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"groups": ("B", "A")}, "groups"),
        ({"groups": ("A",)}, "groups"),
        ({"max_calls": 400}, "max_calls"),
        ({"max_turns": 16}, "max_turns"),
    ],
)
def test_frozen_evaluation_rejects_protocol_drift(frozen, settings, overrides, field):
    with pytest.raises(ValueError, match=field):
        validate(frozen[0], settings, **overrides)


@pytest.mark.parametrize("field,value", [("name", "another-model"), ("timeout", 10.0)])
def test_frozen_evaluation_rejects_model_drift(frozen, settings, field, value):
    changed = settings.model_copy(update={field: value})
    with pytest.raises(ValueError, match="model"):
        validate(frozen[0], changed)


@pytest.mark.parametrize("field", ["implementation_sha256", "packages", "python_version"])
def test_frozen_evaluation_rejects_runtime_drift(frozen, settings, monkeypatch, field):
    current = source_manifest()
    current[field] = {"changed": "1.0"} if field == "packages" else "changed"
    monkeypatch.setattr(protocol, "source_manifest", lambda: current)
    with pytest.raises(ValueError, match=field):
        validate(frozen[0], settings)
    with pytest.raises(ValueError, match="runtime changed"):
        protocol.verify_runtime(frozen[1])


@pytest.mark.parametrize("change", ["schema", "digest", "extra", "provenance", "archive"])
def test_frozen_evaluation_rejects_tampering(frozen, settings, change):
    path, record = frozen
    if change == "schema":
        record["schema_version"] = 0
    elif change == "digest":
        record["protocol"]["max_calls"] = 900
    elif change == "extra":
        record["protocol"]["unregistered_option"] = True
        record["freeze_id"] = fingerprint(record["protocol"])
    elif change == "provenance":
        record["provenance"]["files"] = {}
    else:
        (path.parent / "source.tar.gz").write_bytes(b"changed")
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        validate(path, settings)


def test_execution_claim_is_exclusive_across_competing_workers(frozen, tmp_path):
    path, record = frozen

    def attempt(index):
        try:
            with protocol.claim_execution(path, record, tmp_path / f"run-{index}") as execution:
                execution.update(status="completed", started_episodes=24)
                return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert sum(results) == 1
    saved = read_json(path.parent / "execution.json")
    assert saved["status"] == "completed"
    assert saved["freeze_id"] == record["freeze_id"]
    assert saved["started_episodes"] == 24
    assert saved["finished_at"] >= saved["started_at"]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_interrupted_execution_preserves_claim_and_redacts_error(frozen, tmp_path, error):
    path, record = frozen
    with pytest.raises(error):
        with protocol.claim_execution(path, record, tmp_path / "interrupted"):
            raise error("synthetic-private-error-content")
    claim_path = path.parent / "execution.json"
    saved = read_json(claim_path)
    assert saved["status"] == "interrupted"
    assert saved["error_type"] == error.__name__
    assert "synthetic-private-error-content" not in claim_path.read_text()
    with pytest.raises(ValueError, match="already has an execution"):
        with protocol.claim_execution(path, record, tmp_path / "second-attempt"):
            pytest.fail("An interrupted freeze was executed twice.")


def test_existing_output_is_rejected_before_claim(frozen, tmp_path):
    path, record = frozen
    with pytest.raises(FileExistsError):
        with protocol.claim_execution(path, record, tmp_path):
            pytest.fail("Existing output was accepted.")
    assert not (path.parent / "execution.json").exists()


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.asyncio
async def test_holdout_orchestration_uses_verified_freeze_once(
    frozen, settings, tmp_path, monkeypatch, completed
):
    path, record = frozen
    result = {
        "completed": completed,
        "planned_episodes": 24,
        "started_episodes": 24 if completed else 1,
    }
    runner = AsyncMock(return_value=result)
    monkeypatch.setattr(evaluation, "_run_evaluation", runner)
    output = tmp_path / "holdout"
    assert (
        await evaluation.run_evaluation(
            SOURCE, output, split="holdout", live=True, settings=settings, freeze=path
        )
        == result
    )
    assert runner.await_args.kwargs["frozen"] == record
    assert runner.await_args.kwargs["split"] == "holdout"
    assert read_json(path.parent / "execution.json")["status"] == (
        "completed" if completed else "partial"
    )
    with pytest.raises(ValueError, match="already has an execution"):
        await evaluation.run_evaluation(
            SOURCE,
            tmp_path / "another-output",
            split="holdout",
            live=True,
            settings=settings,
            freeze=path,
        )
    assert runner.await_count == 1


@pytest.mark.asyncio
async def test_invalid_holdout_freeze_stops_before_model_or_task_execution(
    frozen, settings, tmp_path, monkeypatch
):
    runner = AsyncMock()
    monkeypatch.setattr(evaluation, "_run_evaluation", runner)
    with pytest.raises(ValueError, match="max_calls"):
        await evaluation.run_evaluation(
            SOURCE,
            tmp_path / "rejected",
            split="holdout",
            live=True,
            settings=settings,
            freeze=frozen[0],
            max_calls=301,
        )
    runner.assert_not_awaited()
    assert not (frozen[0].parent / "execution.json").exists()


@pytest.mark.asyncio
async def test_development_rejects_holdout_freeze(frozen, settings, tmp_path):
    with pytest.raises(ValueError, match="only for the holdout"):
        await evaluation.run_evaluation(
            SOURCE,
            tmp_path / "rejected",
            split="development",
            live=True,
            settings=settings,
            freeze=frozen[0],
        )
