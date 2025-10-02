#!/bin/bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
  echo -e "${GREEN}$1${NC}"
}

log_warn() {
  echo -e "${YELLOW}$1${NC}"
}

log_error() {
  echo -e "${RED}$1${NC}"
}

usage() {
  cat <<USAGE
Usage: $0 <dev|prod> [--minimal] [--profiles profile1,profile2,...]

Examples:
  $0 dev --minimal
  $0 dev --profiles proxies,monitoring
  $0 prod

Options:
  --minimal          Use docker-compose.min.yml (dashboard only)
  --profiles list    Compose profiles to enable (comma separated)
  -h, --help         Show this help
USAGE
}

MODE=""
MINIMAL=0
PROFILE_LIST=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    dev|prod)
      MODE="$1"
      shift
      ;;
    --minimal)
      MINIMAL=1
      shift
      ;;
    --profiles)
      if [[ $# -lt 2 ]]; then
        log_error "--profiles требует список через запятую"
        usage
        exit 1
      fi
      IFS=',' read -r -a PROFILE_LIST <<< "$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_error "Неизвестный аргумент: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  usage
  exit 1
fi

if [[ $MINIMAL -eq 1 && ${#PROFILE_LIST[@]} -gt 0 ]]; then
  log_warn "--profiles игнорируются в минимальном режиме"
  PROFILE_LIST=()
fi

check_prereqs() {
  log_info "Проверяем зависимости Docker..."
  if ! command -v docker >/dev/null 2>&1; then
    log_error "Docker не установлен"
    exit 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    log_error "Требуется Docker Compose V2 (команда 'docker compose')"
    exit 1
  fi

  if [[ $MINIMAL -eq 0 ]] && ! command -v openssl >/dev/null 2>&1; then
    log_warn "openssl не найден — пропускаем генерацию секретов"
  fi
}

prepare_env() {
  log_info "Готовим .env"
  if [[ ! -f .env ]]; then
    cp .env.example .env
    log_info "Создан .env из .env.example"
  fi

  if [[ "$MODE" == "dev" ]]; then
    if grep -q "^PORT=" .env; then
      sed -i.bak "s|^PORT=.*|PORT=3050|" .env
    else
      echo "PORT=3050" >> .env
    fi

    if grep -q "^NEXT_PUBLIC_APP_URL=" .env; then
      sed -i.bak "s|^NEXT_PUBLIC_APP_URL=.*|NEXT_PUBLIC_APP_URL=http://localhost:3050|" .env
    else
      echo "NEXT_PUBLIC_APP_URL=http://localhost:3050" >> .env
    fi
  fi

  if [[ $MINIMAL -eq 0 ]] && command -v openssl >/dev/null 2>&1; then
    if ! grep -q "JWT_SECRET" .env || grep -q "change-this" .env; then
      secret=$(openssl rand -base64 32)
      sed -i.bak "s|JWT_SECRET=.*|JWT_SECRET=${secret}|" .env
      log_info "Сгенерирован JWT_SECRET"
    fi

    if ! grep -q "REDIS_PASSWORD" .env || grep -q "your-redis-password" .env; then
      redis_pw=$(openssl rand -hex 16)
      sed -i.bak "s|REDIS_PASSWORD=.*|REDIS_PASSWORD=${redis_pw}|" .env
      log_info "Сгенерирован Redis пароль"
    fi
  fi

  rm -f .env.bak
}

ensure_directories() {
  log_info "Проверяем каталоги данных"
  mkdir -p data/database data/sites logs config monitoring ssl backups
  chmod 755 data logs config
  chmod 700 ssl

  if [[ $MINIMAL -eq 0 && ( ! -f ssl/fullchain.pem || ! -f ssl/privkey.pem ) ]]; then
    if command -v openssl >/dev/null 2>&1; then
      log_warn "Генерируем self-signed сертификаты"
      openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout ssl/privkey.pem -out ssl/fullchain.pem -days 365 \
        -subj "/C=US/ST=CA/L=SanFrancisco/O=Scraper/OU=Dev/CN=localhost"
    else
      log_warn "openssl недоступен — пропускаем генерацию сертификатов"
    fi
  fi
}

compose_files=()
if [[ $MINIMAL -eq 1 ]]; then
  compose_files=(-f docker-compose.min.yml)
else
  if [[ "$MODE" == "prod" ]]; then
    compose_files=(-f docker-compose.prod.yml)
  else
    compose_files=(-f docker-compose.yml)
  fi
fi

compose_args=(docker compose "${compose_files[@]}")
for profile in "${PROFILE_LIST[@]}"; do
  compose_args+=(--profile "$profile")
done

compose_down() {
  log_warn "Прерывание — останавливаем контейнеры"
  "${compose_args[@]}" down --remove-orphans || true
}

trap compose_down ERR

check_prereqs
prepare_env
ensure_directories

if [[ $MINIMAL -eq 0 ]]; then
  log_info "Служебные профили: ${PROFILE_LIST[*]:-none}"
fi

if [[ "$MODE" == "prod" ]]; then
  log_info "Собираем образы (prod)"
  "${compose_args[@]}" build --no-cache
else
  log_info "Собираем образы"
  "${compose_args[@]}" build
fi

log_info "Запускаем стэк"
"${compose_args[@]}" up -d

if command -v curl >/dev/null 2>&1; then
  log_info "Проверяем доступность http://localhost:3050/api/health"
  for _ in {1..15}; do
    if curl -fsS http://localhost:3050/api/health >/dev/null 2>&1; then
      log_info "Dashboard доступен по http://localhost:3050"
      exit 0
    fi
    sleep 2
  done
  log_warn "Не удалось подтвердить /api/health. Проверьте 'docker compose logs dashboard'"
else
  log_warn "curl не найден — пропустили проверку здоровья"
fi

log_info "Готово!"
