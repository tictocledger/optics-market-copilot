#!/usr/bin/env bash
# Serve the Optics Market Cockpit static dashboard locally.
#
# Port 8877 is reserved for this repo only. Sibling apps under ~/projects
# already own the ports listed in SIBLING_PORTS — this script refuses those.
#
#   ./run.sh                  # http://127.0.0.1:8877/
#   PORT=8880 ./run.sh        # override, as long as it is not a sibling port
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8877}"
KILL_PORT="${KILL_PORT:-1}"

# Ports already claimed by other ~/projects apps. Keep 8877 out of this list.
SIBLING_PORTS=(
  5173
  8000 8001 8010
  8321 8340 8341
  8471 8472 8501 8502 8510 8511
  8580 8581 8610 8651 8652 8661 8662
  8695 8696 8731 8732 8743 8750 8752 8765
  8860 8920 8921 8930 8931 8960 8961
  8980 8981
  9000 9001 9002
)

refuse_sibling_port() {
  local p
  for p in "${SIBLING_PORTS[@]}"; do
    if [[ "$PORT" == "$p" ]]; then
      echo "Port ${PORT} belongs to another app under ~/projects." >&2
      echo "This repo uses 8877. Unset PORT or pick a free unused port." >&2
      exit 1
    fi
  done
}

free_port() {
  local port="$1"
  local pids=""
  if command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "${port}/tcp" 2>/dev/null || true)"
  elif command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -t -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  fi
  pids="$(echo "$pids" | tr -s '[:space:]' ' ' | xargs || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "Port ${port} is in use (PIDs: ${pids}). Stopping those listeners..."
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
}

refuse_sibling_port

if [[ "$KILL_PORT" == "1" ]]; then
  free_port "$PORT"
fi

WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo "Optics Market Cockpit (static UI)"
echo "  local  → http://127.0.0.1:${PORT}/"
echo "  alias  → http://localhost:${PORT}/"
if [[ -n "$WSL_IP" ]]; then
  echo "  WSL    → http://${WSL_IP}:${PORT}/"
fi
echo "Do not open http://0.0.0.0:${PORT}/ — browsers reject that bind address."
echo "Ctrl+C stops the server."
echo

export PYTHONUNBUFFERED=1
exec python3 -m http.server "$PORT" --bind "$HOST" --directory "$ROOT"
