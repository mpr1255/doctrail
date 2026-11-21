#!/usr/bin/env bash
# Rebuild doctrail's Rust extraction extension (`doctrail._ingest_native`) in
# release mode and install it into the local editable environment.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

echo "building native extraction extension (release)..."
if [ ! -d "$here/.venv" ]; then
    uv sync --no-install-project
fi
uv run --no-project --with 'maturin>=1.10,<2' env VIRTUAL_ENV="$here/.venv" maturin develop --release --uv

"$here/.venv/bin/python" -c "import doctrail._ingest_native as n; assert hasattr(n, 'extract_batch') and hasattr(n, 'bundled_llms_txt'); print('OK: doctrail._ingest_native built and importable')"
