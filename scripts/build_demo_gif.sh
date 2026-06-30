#!/usr/bin/env bash
# Render a demo GIF used in README.md / docs.
# Requires: vhs (brew install vhs), bat, sqlite3, uv.
#
# Renders against the LOCAL source via the repo's dev wrapper (doctrail.py),
# so it needs no network and no wheel build — we are developing this package,
# not installing it from an index. A `doctrail` shim on PATH points the tape's
# commands at the current working tree.
#
# Usage:
#   scripts/build_demo_gif.sh                                   # fed tutorial demo -> docs/assets/demo.gif
#   scripts/build_demo_gif.sh <tape> <warm-corpus> <output>     # any tape
# Example:
#   scripts/build_demo_gif.sh scripts/demo_econ_threat.tape econ-threat docs/assets/demo-econ-threat.gif
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
invocation_dir="$PWD"
tape="${1:-$root/scripts/demo.tape}"
warm_corpus="${2:-fed}"
output="${3:-$root/docs/assets/demo.gif}"

# Resolve tape/output to absolute paths; vhs runs from a scratch cwd.
abspath() { case "$1" in /*) printf '%s' "$1";; *) printf '%s/%s' "$invocation_dir" "$1";; esac; }
tape="$(abspath "$tape")"
output="$(abspath "$output")"
work="$(mktemp -d)"

mkdir "$work/bin"
cat > "$work/bin/doctrail" <<EOF
#!/usr/bin/env bash
exec "$root/doctrail.py" "\$@"
EOF
chmod +x "$work/bin/doctrail"

mkdir "$work/rec"

# Warm the dev wrapper once (resolves the uv environment) so the first
# recorded command responds promptly instead of pausing.
(cd "$(mktemp -d)" && PATH="$work/bin:$PATH" doctrail init test "$warm_corpus" >/dev/null 2>&1) || true

cd "$work/rec"
BAT_PAGER= PATH="$work/bin:$PATH" DOCTRAIL_INGEST_THROTTLE=0.25 vhs "$tape"

gif="$(ls "$work/rec"/*.gif | head -1)"
cp "$gif" "$output"
echo "Wrote $output"
