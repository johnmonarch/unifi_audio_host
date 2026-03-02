#!/bin/sh
set -eu

WATCHER_NAMES_VALUE="${WATCHER_NAMES:-zone1,zone2}"
WATCHER_NAMES_SPACED="$(echo "$WATCHER_NAMES_VALUE" | tr ',' ' ')"

PIDS=""

for watcher_name in $WATCHER_NAMES_SPACED; do
  watcher_name="$(echo "$watcher_name" | xargs)"
  if [ -z "$watcher_name" ]; then
    continue
  fi
  echo "$(date '+%Y-%m-%dT%H:%M:%S') launcher-start watcher=${watcher_name}"
  WATCHER_NAME="$watcher_name" python /app/watcher.py &
  PIDS="$PIDS $!"
done

if [ -z "${PIDS# }" ]; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') launcher-error detail=no_watcher_names"
  exit 1
fi

trap 'kill $PIDS 2>/dev/null || true' INT TERM
wait
