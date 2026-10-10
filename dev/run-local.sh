#!/bin/sh
# Run the agent locally against the public scouting sites (headless) and record the run in experiments/.
# Usage, from anywhere: dev/run-local.sh <version> ["note"]
# Needs `atira login` and `atira models proxy` running in another terminal. HEADLESS=0 shows the browser.
cd "$(dirname "$0")/.." || exit 1
version=${1:?usage: dev/run-local.sh <version> [note]}
note=${2:-}
eval "$(atira scout --env)"
(cd agent && HEADLESS=${HEADLESS:-1} uv run --no-sync python agent.py)
status=$?
python3 dev/track.py local --version "$version" --note "$note" && python3 dev/track.py report
exit $status
