#!/bin/sh
set -eu

CONFIG_FILE="${CONFIG_FILE:-/config/alerts.json}"
RECONCILE_SEC="${LAUNCHER_RECONCILE_SEC:-5}"
PID_DIR="/tmp/watchers"
mkdir -p "$PID_DIR"

log() {
  echo "$(date '+%Y-%m-%dT%H:%M:%S') $*"
}

sanitize_value() {
  value="$(printf '%s' "$1" | xargs)"
  case "$value" in
    ""|"$"|\$\{*\})
      printf ''
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

watchers_from_env() {
  raw="${WATCHER_NAMES:-zone1,zone2}"
  raw="$(sanitize_value "$raw")"
  if [ -z "$raw" ]; then
    raw="zone1,zone2"
  fi

  oldifs="$IFS"
  IFS=','
  set -- $raw
  IFS="$oldifs"

  for watcher in "$@"; do
    watcher="$(sanitize_value "$watcher")"
    if [ -n "$watcher" ]; then
      printf '%s\n' "$watcher"
    fi
  done
}

watchers_from_config() {
  if [ ! -f "$CONFIG_FILE" ]; then
    return 0
  fi

  python - "$CONFIG_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception:
    raise SystemExit(0)

if not isinstance(payload, dict):
    raise SystemExit(0)

watchers = payload.get("watchers", payload)
if not isinstance(watchers, dict):
    raise SystemExit(0)

for name in watchers.keys():
    if isinstance(name, str):
        name = name.strip()
        if name:
            print(name)
PY
}

build_desired_file() {
  out_file="$1"
  : > "$out_file"
  {
    watchers_from_env
    watchers_from_config
  } | awk 'NF && !seen[$0]++ { print $0 }' > "$out_file"
}

pid_file_for() {
  name="$1"
  safe_name="$(printf '%s' "$name" | tr -c 'A-Za-z0-9_.-' '_')"
  printf '%s/%s.pid' "$PID_DIR" "$safe_name"
}

name_file_for() {
  name="$1"
  safe_name="$(printf '%s' "$name" | tr -c 'A-Za-z0-9_.-' '_')"
  printf '%s/%s.name' "$PID_DIR" "$safe_name"
}

start_watcher() {
  name="$1"
  pid_file="$(pid_file_for "$name")"
  name_file="$(name_file_for "$name")"

  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file" "$name_file"
  fi

  log "launcher-start watcher=${name}"
  WATCHER_NAME="$name" python /app/watcher.py &
  pid="$!"
  printf '%s\n' "$pid" > "$pid_file"
  printf '%s\n' "$name" > "$name_file"
}

stop_watcher() {
  name="$1"
  pid_file="$(pid_file_for "$name")"
  name_file="$(name_file_for "$name")"

  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      log "launcher-stop watcher=${name}"
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pid_file" "$name_file"
}

cleanup_all() {
  for pid_file in "$PID_DIR"/*.pid; do
    [ -e "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}

trap 'cleanup_all; exit 0' INT TERM

while true; do
  desired_file="$(mktemp)"
  build_desired_file "$desired_file"

  if [ ! -s "$desired_file" ]; then
    log "launcher-warning detail=no_watcher_names"
  fi

  while IFS= read -r name || [ -n "$name" ]; do
    name="$(sanitize_value "$name")"
    [ -n "$name" ] || continue
    start_watcher "$name"
  done < "$desired_file"

  for pid_file in "$PID_DIR"/*.pid; do
    [ -e "$pid_file" ] || continue
    name_file="${pid_file%.pid}.name"
    name="$(cat "$name_file" 2>/dev/null || true)"
    pid="$(cat "$pid_file" 2>/dev/null || true)"

    if [ -z "$name" ] || [ -z "$pid" ]; then
      rm -f "$pid_file" "$name_file"
      continue
    fi

    if ! grep -Fxq "$name" "$desired_file"; then
      stop_watcher "$name"
      continue
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      log "launcher-exit watcher=${name} detail=process_exited"
      rm -f "$pid_file" "$name_file"
      start_watcher "$name"
    fi
  done

  rm -f "$desired_file"
  sleep "$RECONCILE_SEC"
done
