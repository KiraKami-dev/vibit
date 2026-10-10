#!/bin/sh
# Local measuring loop while the sandboxes are busy: run the agent on the scouting sites up to the signing step
# (never requests a TAN: the webmail is shared by all teams), score its answer with checks/score.py (the
# hand-derived public-1 answer, gitignored) and log one line per run to experiments/local-soak.log.
# Usage, from the repo root: dev/local-soak.sh [runs] ["note"]   (needs `atira models proxy` and checks/)
cd "$(dirname "$0")/.." || exit 1
runs=${1:-1000}
note=${2:-local soak}
i=0
while [ "$i" -lt "$runs" ]; do
  i=$((i + 1))
  commit=$(git rev-parse --short HEAD)
  dir="$PWD/agent/traces/local-$(date -u +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$dir"
  started=$(date +%s)
  eval "$(atira scout --env </dev/null)"
  (cd agent && uv sync -q && HEADLESS=1 AGENT_STOP_BEFORE_SIGN=1 TRACE_DIR="$dir" uv run --no-sync python agent.py \
    > "$dir/stdout.log" 2>&1)
  seconds=$(( $(date +%s) - started ))
  if [ -f "$dir/answer.json" ]; then
    quality=$(python3 checks/score.py --json "$dir/answer.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['quality'])")
  else
    quality="no-answer"
  fi
  problems=$(grep -E '\] \S+ +(problem|start_failed|crashed)' "$dir/stdout.log" | sed -E 's/ +/ /g' | cut -c1-110 | head -4 | tr '\n' '|')
  echo "$(date +%H:%M) $commit local quality=$quality ${seconds}s $note $problems" >> experiments/local-soak.log
done
