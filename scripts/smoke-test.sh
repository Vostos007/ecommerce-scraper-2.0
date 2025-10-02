#!/bin/bash
# =============================================================================
# Smoke Test Script for Webscraper
# =============================================================================
# Быстрая проверка работоспособности всех компонентов системы
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
HOURGLASS="⏳"
INFO="ℹ️"

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

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

print_test() {
    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    echo -e "${BLUE}[TEST $TOTAL_TESTS] $1${NC}"
}

print_success() {
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo -e "${GREEN}  ${CHECK} PASS: $1${NC}"
}

print_failure() {
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo -e "${RED}  ${CROSS} FAIL: $1${NC}"
}

print_info() {
    echo -e "${BLUE}  ${INFO} $1${NC}"
}

test_endpoint() {
    local url=$1
    local expected_pattern=$2
    local description=$3
    
    print_test "$description"
    
    response=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    
    if [ "$response" = "200" ]; then
        if [ -n "$expected_pattern" ]; then
            content=$(curl -s "$url" 2>/dev/null || echo "")
            if echo "$content" | grep -q "$expected_pattern"; then
                print_success "Endpoint доступен и возвращает ожидаемый контент"
                return 0
            else
                print_failure "Endpoint доступен но контент не соответствует ожиданиям"
                print_info "URL: $url"
                return 1
            fi
        else
            print_success "Endpoint доступен (HTTP 200)"
            return 0
        fi
    else
        print_failure "Endpoint недоступен (HTTP $response)"
        print_info "URL: $url"
        return 1
    fi
}

# =============================================================================
# Smoke Tests
# =============================================================================

print_header "🧪 Webscraper Smoke Tests"

# -----------------------------------------------------------------------------
# Test 1: Docker Services Status
# -----------------------------------------------------------------------------
print_test "Проверка статуса Docker сервисов"

if docker compose ps | grep -q "Up"; then
    print_success "Docker сервисы запущены"
else
    print_failure "Docker сервисы не запущены"
    print_info "Запустите: make up"
    exit 1
fi

# -----------------------------------------------------------------------------
# Test 2: PostgreSQL Connectivity
# -----------------------------------------------------------------------------
print_test "Проверка подключения к PostgreSQL"

if docker compose exec -T postgres pg_isready -U scraper 2>/dev/null | grep -q "accepting"; then
    print_success "PostgreSQL отвечает"
else
    print_failure "PostgreSQL не отвечает"
fi

# -----------------------------------------------------------------------------
# Test 3: Redis Connectivity
# -----------------------------------------------------------------------------
print_test "Проверка подключения к Redis"

if docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q "PONG"; then
    print_success "Redis отвечает"
else
    print_failure "Redis не отвечает"
fi

# -----------------------------------------------------------------------------
# Test 4: API Health Endpoint
# -----------------------------------------------------------------------------
test_endpoint "http://localhost:8000/api/health" "healthy" "API Health Endpoint"

# -----------------------------------------------------------------------------
# Test 5: API Root Endpoint
# -----------------------------------------------------------------------------
test_endpoint "http://localhost:8000/" "ok" "API Root Endpoint"

# -----------------------------------------------------------------------------
# Test 6: API Documentation
# -----------------------------------------------------------------------------
test_endpoint "http://localhost:8000/api/docs" "swagger" "API Documentation (Swagger UI)"

# -----------------------------------------------------------------------------
# Test 7: Dashboard Health Endpoint
# -----------------------------------------------------------------------------
test_endpoint "http://localhost:3050/api/health" "" "Dashboard Health Endpoint"

# -----------------------------------------------------------------------------
# Test 8: Database Migrations
# -----------------------------------------------------------------------------
print_test "Проверка применённых миграций"

migration_count=$(docker compose exec -T postgres psql -U scraper -d scraper -t -c "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null | tr -d ' ' || echo "0")

if [ "$migration_count" -gt 0 ]; then
    print_success "Миграции применены ($migration_count шт.)"
else
    print_failure "Миграции не применены"
    print_info "Запустите: make migrate"
fi

# -----------------------------------------------------------------------------
# Test 9: Database Tables
# -----------------------------------------------------------------------------
print_test "Проверка таблиц базы данных"

tables=$(docker compose exec -T postgres psql -U scraper -d scraper -t -c "\dt" 2>/dev/null | grep -c "jobs\|pages\|snapshots\|exports\|metrics" || echo "0")

if [ "$tables" -ge 5 ]; then
    print_success "Все необходимые таблицы созданы"
else
    print_failure "Не все таблицы созданы (найдено: $tables, ожидается: 5+)"
fi

# -----------------------------------------------------------------------------
# Test 10: Worker Processes
# -----------------------------------------------------------------------------
print_test "Проверка Worker процессов"

worker_count=$(docker compose ps worker 2>/dev/null | grep -c "Up" || echo "0")

if [ "$worker_count" -gt 0 ]; then
    print_success "Worker процессы запущены ($worker_count шт.)"
else
    print_failure "Worker процессы не запущены"
fi

# -----------------------------------------------------------------------------
# Test 11: Create Test Job (Integration Test)
# -----------------------------------------------------------------------------
print_test "Создание тестового Job (интеграционный тест)"

# Get admin token from .env
if [ -f .env ]; then
    ADMIN_TOKEN=$(grep ADMIN_TOKEN .env | cut -d '=' -f2 | tr -d ' "' || echo "dev-admin-token")
else
    ADMIN_TOKEN="dev-admin-token"
fi

# Create test job
job_response=$(curl -s -X POST http://localhost:8000/api/jobs \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -d '{
        "domain": "smoke-test.local",
        "urls": ["https://example.com/test"],
        "options": {"test_mode": true}
    }' 2>/dev/null || echo "")

if echo "$job_response" | grep -q "job_id"; then
    job_id=$(echo "$job_response" | grep -o '"job_id":"[^"]*"' | cut -d'"' -f4)
    print_success "Тестовый Job создан (ID: $job_id)"
    
    # Wait a bit and check status
    sleep 2
    
    job_status=$(curl -s "http://localhost:8000/api/jobs/$job_id" 2>/dev/null || echo "")
    
    if echo "$job_status" | grep -q "queued\|running"; then
        print_success "Job находится в обработке"
    else
        print_info "Job status: $(echo $job_status | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
    fi
else
    print_failure "Не удалось создать тестовый Job"
    print_info "Response: $job_response"
fi

# -----------------------------------------------------------------------------
# Test 12: RQ Queue Status
# -----------------------------------------------------------------------------
print_test "Проверка RQ очередей в Redis"

queue_info=$(docker compose exec -T redis redis-cli KEYS "rq:*" 2>/dev/null | wc -l || echo "0")

if [ "$queue_info" -gt 0 ]; then
    print_success "RQ очереди инициализированы"
else
    print_info "RQ очереди пусты (нормально если нет активных jobs)"
fi

# -----------------------------------------------------------------------------
# Test 13: Log Files
# -----------------------------------------------------------------------------
print_test "Проверка файлов логов"

if [ -d "logs" ]; then
    log_count=$(find logs -type f -name "*.log" 2>/dev/null | wc -l || echo "0")
    if [ "$log_count" -gt 0 ]; then
        print_success "Файлы логов создаются"
    else
        print_info "Файлы логов ещё не созданы"
    fi
else
    print_info "Директория logs не создана"
fi

# -----------------------------------------------------------------------------
# Test 14: Data Directory
# -----------------------------------------------------------------------------
print_test "Проверка директории данных"

if [ -d "data" ]; then
    print_success "Директория data существует"
else
    print_failure "Директория data не найдена"
fi

# -----------------------------------------------------------------------------
# Final Summary
# -----------------------------------------------------------------------------
echo ""
print_header "📊 Test Results Summary"

echo "Total Tests:  $TOTAL_TESTS"
echo -e "${GREEN}Passed:       $PASSED_TESTS${NC}"
echo -e "${RED}Failed:       $FAILED_TESTS${NC}"

if [ $FAILED_TESTS -eq 0 ]; then
    echo ""
    echo -e "${GREEN}${CHECK} Все тесты пройдены успешно!${NC}"
    echo ""
    echo "✨ Система работает корректно и готова к использованию"
    echo ""
    echo "📍 Доступные endpoints:"
    echo "  • API:       http://localhost:8000/api/docs"
    echo "  • Dashboard: http://localhost:3050"
    echo ""
    exit 0
else
    echo ""
    echo -e "${RED}${CROSS} Некоторые тесты провалились${NC}"
    echo ""
    echo "🔧 Попробуйте:"
    echo "  1. Проверить логи: make logs"
    echo "  2. Перезапустить сервисы: make restart"
    echo "  3. Проверить .env конфигурацию"
    echo "  4. Применить миграции: make migrate"
    echo ""
    echo "📚 Документация: cat GETTING_STARTED.md"
    echo ""
    exit 1
fi