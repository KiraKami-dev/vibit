#!/bin/sh
# Run the agent locally against the public scouting sites (headless) and record it in experiments/local-runs.jsonl
# (gitignored). Usage, from anywhere: dev/run-local.sh <version> ["note"] [experiment]
# Needs `atira login` and `atira models proxy` running in another terminal. HEADLESS=0 shows the browser.
cd "$(dirname "$0")/.." || exit 1
version=${1:?usage: dev/run-local.sh <version> [note] [experiment]}
note=${2:-}
experiment=${3:-}
curl -s --max-time 5 -o /dev/null http://127.0.0.1:8765/ || {
  echo "The models proxy is not running: start \`atira models proxy\` in another terminal first."; exit 1; }
commit=$(git rev-parse --short HEAD)
dirty=no; [ -n "$(git status --porcelain -- agent)" ] && dirty=yes
traces="$PWD/agent/traces/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$traces"
eval "$(atira scout --env)"
(cd agent && uv sync -q && HEADLESS=${HEADLESS:-1} TRACE_DIR="$traces" uv run --no-sync python agent.py)
status=$?
trace=$(ls "$traces"/run-*.jsonl 2>/dev/null | head -1)
[ -n "$trace" ] || { echo "The agent wrote no trace; nothing recorded."; exit $status; }
python3 dev/track.py local --trace "$trace" --version "$version" --experiment "$experiment" --note "$note" \
  --commit "$commit" --dirty "$dirty" && python3 dev/track.py report
exit $status
