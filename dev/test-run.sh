#!/bin/sh
# Start a sandbox test run of agent/ (public data, never ranked), follow it to the end and record its score.
# Usage, from anywhere: dev/test-run.sh <version> ["note"] [experiment]
cd "$(dirname "$0")/.." || exit 1
version=${1:?usage: dev/test-run.sh <version> [note]}
note=${2:-}
experiment=${3:-}
log=$(mktemp)
atira test-run ./agent --no-open --follow </dev/null | tee "$log"
run=$(grep -o 'run_[0-9a-f]*' "$log" | head -1)
rm -f "$log"
[ -n "$run" ] || { echo "no run id in the output; record it later with: python3 dev/track.py platform <id>"; exit 1; }
python3 dev/track.py platform "$run" --version "$version" --note "$note" --experiment "$experiment" && python3 dev/track.py report
