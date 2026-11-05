"""Freeze a holdout protocol and preserve a single attempted execution per freeze."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from servicedesk_agent.benchmark import POLICY_PROFILE, subset_manifest, validate_source
from servicedesk_agent.config import ModelSettings
from servicedesk_agent.domain import fingerprint
from servicedesk_agent.provenance import capture_source, source_manifest


def validate_options(groups: tuple[str, ...], max_calls: int, max_turns: int) -> None:
    if not groups or any(group not in {"A", "B"} for group in groups):
        raise ValueError("Select A and/or B.")
    if len(groups) != len(set(groups)):
        raise ValueError("Select each evaluation group once.")
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
        raise ValueError("The model call limit must be a positive integer.")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError("The turn limit must be a positive integer.")


def _protocol(settings, groups, max_calls, max_turns, provenance) -> dict:
    manifest = subset_manifest()
    return {
        "split": "holdout",
        "groups": list(groups),
        "tasks": manifest["holdout"],
        "source_repository": manifest["source_repository"],
        "source_commit": manifest["source_commit"],
        "tasks_sha256": manifest["tasks_sha256"],
        "policy_sha256": manifest["policy_sha256"],
        "seed": manifest["seed"],
        "model": settings.model_dump(mode="json", exclude={"api_key"}),
        "max_calls": max_calls,
        "max_turns": max_turns,
        "implementation_sha256": provenance["implementation_sha256"],
        "packages": provenance["packages"],
        "python_version": provenance["python_version"],
        "user_simulator": "react",
        "semantic_judge_calls": 0,
        "native_summarization": True,
        "policy_profile": POLICY_PROFILE,
        "repeat_policy": "one attempted execution per freeze directory; no automatic retries",
    }


def _write_json(path: Path, value: dict) -> None:
    with path.open("w", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def freeze_evaluation(
    source: Path,
    output: Path,
    *,
    settings: ModelSettings,
    groups: tuple[str, ...] = ("A", "B"),
    max_calls: int = 300,
    max_turns: int = 15,
) -> dict:
    validate_options(groups, max_calls, max_turns)
    settings.require_live()
    validate_source(source)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    provenance = capture_source(output)
    protocol = _protocol(settings, groups, max_calls, max_turns, provenance)
    record = {
        "schema_version": 1,
        "freeze_id": fingerprint(protocol),
        "created_at": datetime.now(UTC).isoformat(),
        "protocol": protocol,
        "provenance": provenance,
    }
    _write_json(output / "freeze.json", record)
    return record


def validate_freeze(
    path: Path,
    source: Path,
    *,
    settings: ModelSettings,
    groups: tuple[str, ...],
    max_calls: int,
    max_turns: int,
) -> dict:
    validate_options(groups, max_calls, max_turns)
    settings.require_live()
    validate_source(source)
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("Use a supported evaluation freeze.")
    protocol = record.get("protocol")
    if not isinstance(protocol, dict) or record.get("freeze_id") != fingerprint(protocol):
        raise ValueError("The evaluation freeze digest is invalid.")
    current = source_manifest()
    expected = _protocol(settings, groups, max_calls, max_turns, current)
    differences = [key for key in expected if protocol.get(key) != expected[key]]
    if differences or set(protocol) != set(expected):
        raise ValueError("Frozen evaluation differs in: " + ", ".join(differences or ["fields"]))
    provenance = record.get("provenance", {})
    if not isinstance(provenance, dict) or any(
        provenance.get(key) != current[key] for key in current
    ):
        raise ValueError("The frozen source manifest differs from the current runtime.")
    archive = path.parent / "source.tar.gz"
    if (
        provenance.get("source_archive") != archive.name
        or not archive.is_file()
        or hashlib.sha256(archive.read_bytes()).hexdigest()
        != provenance.get("source_archive_sha256")
    ):
        raise ValueError("The frozen source archive is missing or changed.")
    return record


def verify_runtime(record: dict) -> None:
    current = source_manifest()
    if any(current[key] != record["provenance"][key] for key in current):
        raise ValueError("The runtime changed after the holdout protocol was frozen.")


@contextmanager
def claim_execution(path: Path, record: dict, output: Path):
    if output.exists():
        raise FileExistsError(f"Evaluation output already exists: {output}")
    claim_path = path.parent / "execution.json"
    claim = {
        "freeze_id": record["freeze_id"],
        "output": str(output.resolve()),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
    }
    try:
        descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("This freeze already has an execution; inspect execution.json.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(claim, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield claim
    except BaseException as exc:
        claim.update(status="interrupted", error_type=type(exc).__name__)
        raise
    finally:
        claim["finished_at"] = datetime.now(UTC).isoformat()
        _write_json(claim_path, claim)
