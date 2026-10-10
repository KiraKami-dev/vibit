#!/bin/sh
# Start a sandbox test run of agent/ (public data, never ranked), follow it to the end and record its score in
# experiments/runs.jsonl (commit that file afterwards). Usage, from anywhere: dev/test-run.sh <version> ["note"] [experiment]
cd "$(dirname "$0")/.." || exit 1
version=${1:?usage: dev/test-run.sh <version> [note] [experiment]}
note=${2:-}
experiment=${3:-}
commit=$(git rev-parse --short HEAD)
dirty=no; [ -n "$(git status --porcelain -- agent)" ] && dirty=yes
log=$(mktemp)
record="python3 dev/track.py platform RUN --version '$version' --experiment '$experiment' --note '$note' --commit $commit --dirty $dirty"
trap 'run=$(grep -o "run_[0-9a-f]*" "$log" | head -1); echo; echo "Stopped following; the run goes on. Record it once it ends:"; echo "  $record" | sed "s/RUN/${run:-<run id>}/"; exit 130' INT
atira test-run ./agent --no-open --follow </dev/null | tee "$log"
run=$(grep -o 'run_[0-9a-f]*' "$log" | head -1)
rm -f "$log"
[ -n "$run" ] || { echo "No run id in the output; record it later with: $record"; exit 1; }
eval "$(echo "$record" | sed "s/RUN/$run/")" && python3 dev/track.py report && python3 dev/dashboard.py
