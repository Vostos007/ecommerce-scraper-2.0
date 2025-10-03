#!/bin/bash
# =============================================================================
# Dev Environment Setup Script for Webscraper
# =============================================================================
# Этот скрипт выполняет первичную настройку development окружения
# =============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Emojis
CHECK="✅"
CROSS="❌"
ROCKET="🚀"
WRENCH="🔧"
INFO="ℹ️"

# =============================================================================
# Helper Functions
# =============================================================================

print_header() {
    echo ""
    echo -e "${BLUE}==============================================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}==============================================================================${NC}"
    echo ""
}

print_step() {
    echo -e "${GREEN}${WRENCH} $1${NC}"
}

print_success() {
    echo -e "${GREEN}${CHECK} $1${NC}"
}

print_error() {
    echo -e "${RED}${CROSS} $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_info() {
    echo -e "${BLUE}${INFO} $1${NC}"
}

check_command() {
    if command -v $1 &> /dev/null; then
        print_success "$1 найден: $(command -v $1)"
        return 0
    else
        print_error "$1 не найден"
        return 1
    fi
}

# =============================================================================
# Main Script
# =============================================================================

print_header "${ROCKET} Webscraper Development Setup"

# -----------------------------------------------------------------------------
# Step 1: Check Prerequisites
# -----------------------------------------------------------------------------
print_step "Шаг 1: Проверка prerequisites..."

MISSING_DEPS=0

# Check Docker
if check_command docker; then
    docker --version
else
    MISSING_DEPS=1
fi

# Check Docker Compose
if check_command "docker compose" || check_command "docker-compose"; then
    if command -v "docker compose" &> /dev/null; then
        docker compose version
    else
        docker-compose --version
    fi
else
    MISSING_DEPS=1
fi

# Check Python (обязателен для локального запуска скриптов)
if check_command python3; then
    PY_VERSION=$(python3 -c 'import sys; print("%s.%s.%s" % sys.version_info[:3])')
    echo "python3 ${PY_VERSION}"
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
        print_error "Требуется Python 3.11+, обнаружено ${PY_VERSION}"
        MISSING_DEPS=1
    fi
else
    print_warning "Python3 не найден (обязателен для локальных скриптов). Установите Python 3.11+"
    MISSING_DEPS=1
fi

# Check Node.js (optional for dashboard dev)
if check_command node; then
    node --version
else
    print_warning "Node.js не найден (опционально для Docker-based запуска)"
fi

if [ $MISSING_DEPS -eq 1 ]; then
    echo ""
    print_error "Некоторые обязательные зависимости не найдены!"
    print_info "Установите Docker и Docker Compose: https://docs.docker.com/get-docker/"
    exit 1
fi

print_success "Все обязательные зависимости установлены"
echo ""

# -----------------------------------------------------------------------------
# Step 2: Setup .env file
# -----------------------------------------------------------------------------
print_step "Шаг 2: Настройка .env файла..."

if [ -f .env ]; then
    print_warning ".env файл уже существует"
    read -p "Перезаписать? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cp .env.example .env
        print_success ".env файл перезаписан из .env.example"
    else
        print_info "Используем существующий .env файл"
    fi
else
    if [ -f .env.example ]; then
        cp .env.example .env
        print_success ".env файл создан из .env.example"
    else
        print_error ".env.example не найден!"
        exit 1
    fi
fi

echo ""

# -----------------------------------------------------------------------------
# Step 3: Create necessary directories
# -----------------------------------------------------------------------------
print_step "Шаг 3: Создание необходимых директорий..."

mkdir -p data/jobs
mkdir -p data/exports
mkdir -p logs
mkdir -p backups

print_success "Директории созданы"
echo ""

# -----------------------------------------------------------------------------
# Step 4: Pull/Build Docker images
# -----------------------------------------------------------------------------
print_step "Шаг 4: Подготовка Docker образов..."

print_info "Скачиваем базовые образы (PostgreSQL, Redis)..."
docker compose pull postgres redis 2>&1 | grep -v "Pulling" || true

print_info "Собираем custom образы (API, Worker, Dashboard)..."
docker compose build --no-cache 2>&1 | tail -n 5

print_success "Docker образы готовы"
echo ""

# -----------------------------------------------------------------------------
# Step 5: Start services
# -----------------------------------------------------------------------------
print_step "Шаг 5: Запуск сервисов..."

docker compose up -d

print_info "Ожидаем готовности сервисов (30 секунд)..."
sleep 30

print_success "Сервисы запущены"
echo ""

# -----------------------------------------------------------------------------
# Step 6: Run migrations
# -----------------------------------------------------------------------------
print_step "Шаг 6: Применение миграций базы данных..."

# Wait for postgres to be fully ready
print_info "Ожидаем готовности PostgreSQL..."
for i in {1..30}; do
    if docker compose exec -T postgres pg_isready -U scraper &> /dev/null; then
        print_success "PostgreSQL готов"
        break
    fi
    if [ $i -eq 30 ]; then
        print_error "PostgreSQL не отвечает после 30 попыток"
        exit 1
    fi
    sleep 1
done

print_info "Применяем миграции..."
docker compose exec api python network/NEW_PROJECT/database/migrate.py

print_success "Миграции применены"
echo ""

# -----------------------------------------------------------------------------
# Step 7: Verify installation
# -----------------------------------------------------------------------------
print_step "Шаг 7: Проверка установки..."

echo ""
print_info "Проверяем endpoints..."

# Check API
if curl -s http://localhost:8000/api/health | grep -q "healthy"; then
    print_success "API работает: http://localhost:8000"
else
    print_warning "API ещё не готов (может потребоваться время)"
fi

# Check Dashboard
if curl -s http://localhost:3050/api/health | grep -q "ok"; then
    print_success "Dashboard работает: http://localhost:3050"
else
    print_warning "Dashboard ещё не готов (может потребоваться время)"
fi

# Check Redis
if docker compose exec -T redis redis-cli ping | grep -q "PONG"; then
    print_success "Redis работает"
else
    print_error "Redis не отвечает"
fi

# Check Postgres
if docker compose exec -T postgres pg_isready -U scraper | grep -q "accepting"; then
    print_success "PostgreSQL работает"
else
    print_error "PostgreSQL не отвечает"
fi

echo ""

# -----------------------------------------------------------------------------
# Final Summary
# -----------------------------------------------------------------------------
print_header "${ROCKET} Setup Complete!"

echo -e "${GREEN}Ваше dev окружение готово к работе!${NC}"
echo ""
echo "📍 Доступные endpoints:"
echo "  • API Documentation:  http://localhost:8000/api/docs"
echo "  • Dashboard UI:       http://localhost:3050"
echo "  • Grafana (optional): http://localhost:3001"
echo ""
echo "🔧 Полезные команды:"
echo "  • make logs        - Посмотреть логи"
echo "  • make status      - Проверить статус сервисов"
echo "  • make smoke-test  - Запустить smoke test"
echo "  • make help        - Показать все доступные команды"
echo ""
echo "📚 Документация:"
echo "  • Quick Start: cat GETTING_STARTED.md"
echo "  • API Docs:    open http://localhost:8000/api/docs"
echo ""
echo -e "${YELLOW}⚠️  Для production deployment измените секреты в .env файле!${NC}"
echo ""
echo -e "${GREEN}${ROCKET} Happy coding!${NC}"
echo ""
