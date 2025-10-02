#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.flaresolverr.yml"
SERVICE_NAME="flaresolverr"
HOST="127.0.0.1"
PORT="8192"
HEALTH_ENDPOINT="http://${HOST}:${PORT}/health"
COMPOSE_BIN=()

usage() {
  cat <<EOF
Usage: $0 [command]

Commands:
  install       Pull latest FlareSolverr image and start via docker-compose
  start         Start the FlareSolverr service
  stop          Stop the FlareSolverr service
  restart       Restart the FlareSolverr service
  status        Show service status and health information
  logs          Tail container logs
  update        Pull latest image and recreate container
  test          Run a simple health-check request against the API

Examples:
  $0 install
  $0 status
  $0 test
EOF
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[error] Docker is required. Install Docker and retry." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "[error] Docker daemon is not running or accessible." >&2
    exit 1
  fi

  if docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_BIN=(docker-compose)
  else
    echo "[error] Neither 'docker compose' nor 'docker-compose' is available." >&2
    exit 1
  fi
}

require_compose_file() {
  if [[ ! -f ${COMPOSE_FILE} ]]; then
    echo "[error] ${COMPOSE_FILE} not found. Run from repository root or adjust COMPOSE_FILE." >&2
    exit 1
  fi
}

compose() {
  "${COMPOSE_BIN[@]}" -f "${COMPOSE_FILE}" "$@"
}

health_check() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "${HEALTH_ENDPOINT}" || return 1
  else
    python - <<PY
import json, sys, urllib.request
try:
    with urllib.request.urlopen('${HEALTH_ENDPOINT}', timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        if data.get('status') != 'ok':
            raise SystemExit(1)
except Exception:
    raise SystemExit(1)
PY
  fi
}

command_install() {
  require_docker
  require_compose_file
  echo "[info] Pulling FlareSolverr image..."
  docker pull ghcr.io/flaresolverr/flaresolverr:latest
  echo "[info] Starting FlareSolverr container..."
  compose up -d
  echo "[info] Waiting for service to become healthy..."
  if health_check; then
    echo "[info] FlareSolverr is healthy at ${HEALTH_ENDPOINT}"
  else
    echo "[warn] Health check failed. Inspect logs with '$0 logs'"
    exit 1
  fi
}

command_start() {
  require_docker
  require_compose_file
  compose up -d
}

command_stop() {
  require_docker
  require_compose_file
  compose stop
}

command_restart() {
  command_stop
  sleep 2
  command_start
}

command_status() {
  require_docker
  require_compose_file
  compose ps
  echo "[info] Checking health endpoint ${HEALTH_ENDPOINT}"
  if health_check; then
    echo "[info] Health check OK"
  else
    echo "[warn] Health check failed"
  fi
}

command_logs() {
  require_docker
  require_compose_file
  compose logs -f ${SERVICE_NAME}
}

command_update() {
  require_docker
  require_compose_file
  echo "[info] Pulling latest image..."
  docker pull ghcr.io/flaresolverr/flaresolverr:latest
  compose up -d --remove-orphans
}

command_test() {
  require_docker
  if health_check; then
    echo "[info] FlareSolverr responded successfully"
  else
    echo "[error] FlareSolverr health check failed"
    exit 1
  fi
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

case "$1" in
  install|start|stop|restart|status|logs|update|test)
    cmd="command_$1"
    shift
    "$cmd" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "[error] Unknown command: $1" >&2
    usage
    exit 1
    ;;
fi
