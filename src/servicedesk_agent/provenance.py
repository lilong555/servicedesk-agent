"""Capture the implementation and installed package versions for each evaluation."""

from __future__ import annotations

import hashlib
import tarfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from servicedesk_agent.domain import fingerprint


def capture_source(output: Path) -> dict:
    package = Path(__file__).resolve().parent
    project = package.parent.parent
    sources = {
        f"src/servicedesk_agent/{path.relative_to(package)}": path
        for path in package.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".md"}
    }
    for name in ("pyproject.toml", "uv.lock", ".python-version", ".env.example"):
        path = project / name
        if path.is_file():
            sources[name] = path
    hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(sources.items())
    }
    archive_path = output / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, path in sorted(sources.items()):
            archive.add(path, arcname=name, recursive=False)
        for name in ("scripts/bootstrap.sh", "README.md"):
            path = project / name
            if path.is_file():
                archive.add(path, arcname=name, recursive=False)
    archive_path.chmod(0o600)
    versions = {}
    for name in (
        "deepagents",
        "langchain",
        "langchain-core",
        "langgraph",
        "langgraph-checkpoint-sqlite",
        "langchain-openai",
        "openai",
        "langmem",
        "haystack-ai",
        "mcp",
        "langchain-mcp-adapters",
        "crm-agent-bench",
    ):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            continue
    return {
        "implementation_sha256": fingerprint(hashes),
        "files": hashes,
        "packages": versions,
        "source_archive": archive_path.name,
        "source_archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }
