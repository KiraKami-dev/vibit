#!/bin/sh
# Keeps the shared history filling while nobody has the dashboard open.
# Each call runs the function once (unique query bypasses the edge cache).
URL="${1:-https://error404-competitor-watch.vercel.app}"
while true; do
  curl -fsS -m 15 -A "error404ai-dashboard/0.1 (keepalive)" -o /dev/null "$URL/api/state?k=$(date +%s)" || sleep 10
  sleep 5
done
