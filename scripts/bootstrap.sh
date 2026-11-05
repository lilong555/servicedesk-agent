#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMMIT=fc9575cf9bda085aa1f66d920fea944588005964
SOURCE="$ROOT/vendor/crm-agent-bench"

if [[ ! -d "$SOURCE/.git" ]]; then
    git clone --no-checkout https://github.com/salesforce-misc/crm-agent-bench.git "$SOURCE"
    git -C "$SOURCE" checkout --detach "$COMMIT"
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$COMMIT" ]]; then
    printf '%s\n' "CRM source must be checked out at $COMMIT." >&2
    exit 1
fi
if [[ -n "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ]]; then
    printf '%s\n' "The pinned CRM source contains tracked changes." >&2
    exit 1
fi

if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
elif [[ -x "$ROOT/.tools/uv" ]]; then
    UV="$ROOT/.tools/uv"
else
    "${PYTHON_BIN:-python3}" -m venv "$ROOT/.tools/bootstrap"
    "$ROOT/.tools/bootstrap/bin/python" -m pip install uv==0.13.7
    UV="$ROOT/.tools/bootstrap/bin/uv"
fi
"$UV" sync --all-extras --frozen
"$UV" pip check
