#!/usr/bin/env bash
# Build the doctrail native extraction extension (the vendored Rust ingestor at
# native/ingestor/) and install the compiled abi3 module into the package so the
# editable dev install imports `doctrail._ingest_native` and `--extractor rust`
# works locally.
#
# The .so is gitignored (it is platform/arch specific and ~13M). Run this once
# after cloning, and again after changing anything under native/ingestor/.
# The test suite does NOT need this — it forces the Python path via
# DOCTRAIL_DISABLE_NATIVE in tests/conftest.py so results match CI.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
crate="$here/native/ingestor"
dest="$here/src/doctrail/_ingest_native.abi3.so"
build_dir="$crate/target/doctrail-wheel"
module_tmp="$dest.tmp"
mkdir -p "$build_dir"
trap 'test ! -e "$module_tmp" || rm "$module_tmp"' EXIT

echo "building native extraction extension (release)..."
uv run --with 'maturin>=1.7,<2' maturin build \
    --release \
    --manifest-path "$crate/Cargo.toml" \
    --out "$build_dir"

whl="$(fd --max-depth 1 --type f --extension whl . "$build_dir" | head -1)"
test -n "$whl"
echo "extracting $whl -> $dest"
unzip -p "$whl" '_ingest_native/_ingest_native.abi3.so' > "$module_tmp"
test -s "$module_tmp"
mv "$module_tmp" "$dest"

uv run python -c "import doctrail._ingest_native as n; assert hasattr(n, 'extract_batch'); print('OK: doctrail._ingest_native importable with extract_batch')"
