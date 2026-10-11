#!/bin/sh
# Run the agent locally against the mirror of the recorded portals (sim/mirror.py) instead of the public sites, and
# record it in experiments/local-runs.jsonl (gitignored) with experiment "sim:mirror" unless another is given.
# Usage, from anywhere: dev/run-sim.sh <version> ["note"] [experiment]
# Needs a recording in sim/captured/ (sim/capture.py, sim/drive.py), `atira login` and `atira models proxy` running
# in another terminal. HEADLESS=0 shows the browser; SIM_SESSION_SECONDS and SIM_FLAKY make the mirror harder.
cd "$(dirname "$0")/.." || exit 1
version=${1:?usage: dev/run-sim.sh <version> [note] [experiment]}
note=${2:-}
experiment=${3:-sim:mirror}
[ -d sim/captured ] || { echo "No recording in sim/captured/: record the sites first (see sim/README.md)."; exit 1; }
curl -s --max-time 5 -o /dev/null http://127.0.0.1:8765/ || {
  echo "The models proxy is not running: start \`atira models proxy\` in another terminal first."; exit 1; }
commit=$(git rev-parse --short HEAD)
dirty=no; [ -n "$(git status --porcelain -- agent)" ] && dirty=yes
traces="$PWD/agent/traces/sim-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$traces"
rm -f sim/captured/_missing.log
(cd agent && uv sync -q)
HEADLESS=${HEADLESS:-1} TRACE_DIR="$traces" python3 sim/mirror.py -- \
  sh -c 'cd agent && uv run --no-sync python agent.py'
status=$?
trace=$(ls "$traces"/run-*.jsonl 2>/dev/null | head -1)
[ -n "$trace" ] || { echo "The agent wrote no trace; nothing recorded."; exit $status; }
python3 dev/track.py local --trace "$trace" --version "$version" --experiment "$experiment" --note "$note" \
  --commit "$commit" --dirty "$dirty" && python3 dev/track.py report && python3 dev/dashboard.py
exit $status
