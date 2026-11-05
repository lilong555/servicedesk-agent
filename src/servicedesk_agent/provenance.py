"""Capture the implementation and installed package versions for each evaluation."""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
from importlib.metadata import distributions
from pathlib import Path

from servicedesk_agent.domain import fingerprint


def _source_files() -> dict[str, Path]:
    package = Path(__file__).resolve().parent
    project = package.parent.parent
    sources = {
        f"src/servicedesk_agent/{path.relative_to(package)}": path
        for path in package.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".md"}
    }
    for name in ("pyproject.toml", "uv.lock", ".python-version", ".env.example", "compose.yaml"):
        path = project / name
        if path.is_file():
            sources[name] = path
    scripts = project / "scripts"
    if scripts.is_dir():
        sources.update(
            {
                f"scripts/{path.relative_to(scripts)}": path
                for path in scripts.rglob("*")
                if path.is_file() and path.suffix in {".py", ".sh", ".rb"}
            }
        )
    return sources


def _manifest(contents: dict[str, bytes]) -> dict:
    hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in sorted(contents.items())
    }
    packages = {
        package.metadata["Name"]: package.version
        for package in distributions()
        if package.metadata["Name"]
    }
    return {
        "implementation_sha256": fingerprint(hashes),
        "files": hashes,
        "packages": dict(sorted(packages.items())),
        "python_version": platform.python_version(),
    }


def source_manifest() -> dict:
    return _manifest({name: path.read_bytes() for name, path in _source_files().items()})


def capture_source(output: Path) -> dict:
    contents = {name: path.read_bytes() for name, path in _source_files().items()}
    manifest = _manifest(contents)
    project = Path(__file__).resolve().parent.parent.parent
    archive_path = output / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, content in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
        for name in ("README.md",):
            path = project / name
            if path.is_file():
                archive.add(path, arcname=name, recursive=False)
    archive_path.chmod(0o600)
    return {
        **manifest,
        "source_archive": archive_path.name,
        "source_archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }
