#!/usr/bin/env bash
# ensure_flaresolverr.sh – helper that guarantees FlareSolverr is running locally.
# Usage: source scripts/ensure_flaresolverr.sh && ensure_flaresolverr

set -euo pipefail

COMPOSE_FILE=${FLARESOLVERR_COMPOSE_FILE:-docker-compose.flaresolverr.yml}
FLARESOLVERR_URL=${FLARESOLVERR_URL:-http://localhost:8192}
FLARESOLVERR_HEALTH_ENDPOINT="${FLARESOLVERR_URL%/}/health"
FLARESOLVERR_WAIT_SECONDS=${FLARESOLVERR_WAIT_SECONDS:-30}

ensure_flaresolverr() {
    local deadline message

    if ! docker info >/dev/null 2>&1; then
        echo "[flaresolverr] ❌ Docker daemon недоступен" >&2
        return 1
    fi

    if ! docker ps --format '{{.Names}}' | grep -q '^flaresolverr'; then
        echo "[flaresolverr] 🚀 Запускаю контейнер..."
        docker-compose -f "$COMPOSE_FILE" up -d
    fi

    echo "[flaresolverr] ⏳ Проверяю готовность API (${FLARESOLVERR_HEALTH_ENDPOINT})..."
    deadline=$((SECONDS + FLARESOLVERR_WAIT_SECONDS))
    while (( SECONDS < deadline )); do
        if curl -sf "$FLARESOLVERR_HEALTH_ENDPOINT" | grep -q '"status"\s*:\s*"ok"'; then
            echo "[flaresolverr] ✅ Готов к работе"
            return 0
        fi
        sleep 1
    done

    message="Не удалось получить status=ok от FlareSolverr за ${FLARESOLVERR_WAIT_SECONDS}с"
    echo "[flaresolverr] ❌ $message" >&2
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if ensure_flaresolverr; then
        exit 0
    fi
    exit 1
fi
