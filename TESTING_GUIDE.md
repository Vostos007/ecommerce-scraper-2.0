# Руководство по тестированию

## ✅ Предварительная проверка структуры

Убедитесь, что все файлы на месте:

```bash
cd network/NEW_PROJECT

# Проверить структуру services
ls -la services/api/
ls -la services/worker/

# Проверить наличие конфигурационных файлов
ls -la | grep -E "(Dockerfile|docker-compose|Makefile|.env)"
```

## 🧪 Локальное тестирование (без Docker)

### Шаг 1: Установить зависимости

```bash
cd network/NEW_PROJECT

# Создать виртуальное окружение
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или venv\Scripts\activate  # Windows

# Установить зависимости
pip install -r services/api/requirements.txt
pip install -r services/worker/requirements.txt
playwright install chromium
```

### Шаг 2: Запустить PostgreSQL и Redis локально

```bash
# PostgreSQL
docker run -d --name test_postgres \
  -e POSTGRES_DB=scraper \
  -e POSTGRES_USER=scraper \
  -e POSTGRES_PASSWORD=scraper \
  -p 5432:5432 \
  postgres:15-alpine

# Redis
docker run -d --name test_redis \
  -p 6379:6379 \
  redis:7-alpine
```

### Шаг 3: Настроить переменные окружения

```bash
export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"
export REDIS_URL="redis://localhost:6379/0"
export FLARESOLVERR_URL="http://localhost:8191"
```

### Шаг 4: Применить миграции

```bash
python database/migrate.py
```

**Ожидаемый результат:**
```
Applying migration: 001_create_jobs_schema
✅ Applied: 001_create_jobs_schema
✅ All migrations applied
```

### Шаг 5: Запустить API

В одном терминале:

```bash
cd network/NEW_PROJECT
export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"
export REDIS_URL="redis://localhost:6379/0"

# Запустить API
python -m uvicorn services.api.main:app --reload --port 8000
```

**Ожидаемый результат:**
```
[API] Starting up...
[API] Database URL: postgresql://scraper:scraper@localhost:5432/scraper
[API] Redis URL: redis://localhost:6379/0
[API] Database pool initialized
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Шаг 6: Запустить Worker

В другом терминале:

```bash
cd network/NEW_PROJECT
export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"
export REDIS_URL="redis://localhost:6379/0"

# Запустить Worker
python services/worker/worker.py
```

**Ожидаемый результат:**
```
🚀 Worker started: worker-12345
📡 Listening to queue: scraping
🔗 Redis: redis://localhost:6379/0
```

### Шаг 7: Проверить API

```bash
# Health check
curl http://localhost:8000/api/health

# Ожидаемый результат:
# {"status":"ok","database":"ok"}

# API документация
open http://localhost:8000/api/docs
```

### Шаг 8: Создать тестовую задачу

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "sitemap_urls": ["https://example.com"],
    "options": {
      "domain": "example.com",
      "max_concurrency": 1
    }
  }'
```

**Ожидаемый результат:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "domain": "example.com",
  "status": "queued",
  "created_at": "2025-10-02T05:30:00Z",
  "total_urls": 1,
  "success_urls": 0,
  "failed_urls": 0
}
```

### Шаг 9: Проверить выполнение задачи

```bash
# Получить job_id из предыдущего ответа
JOB_ID="550e8400-e29b-41d4-a716-446655440000"

# Проверить статус
curl http://localhost:8000/api/jobs/$JOB_ID
```

В логах Worker должно появиться:
```
[RQ Task] Starting job 550e8400-... with 1 URLs
[JobExecutor] Processing 1/1: https://example.com
```

## 🐳 Тестирование с Docker Compose

### Шаг 1: Запустить все сервисы

```bash
cd network/NEW_PROJECT
make up
```

**Ожидаемый результат:**
```
🚀 Starting services...
✅ Services started

📡 API:      http://localhost:8000
📚 API Docs: http://localhost:8000/api/docs
🪣 MinIO:    http://localhost:9001 (admin/minioadmin)
```

### Шаг 2: Применить миграции

```bash
make migrate
```

### Шаг 3: Проверить логи

```bash
make logs

# Или отдельные сервисы:
docker-compose logs -f api
docker-compose logs -f worker
```

### Шаг 4: Проверить health endpoints

```bash
# API health
curl http://localhost:8000/api/health

# Проверить MinIO
curl http://localhost:9000/minio/health/live
```

### Шаг 5: Создать тестовую задачу

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "sitemap_urls": ["https://httpbin.org/html"],
    "options": {
      "domain": "httpbin.org",
      "max_concurrency": 1
    }
  }'
```

### Шаг 6: Мониторинг выполнения

```bash
# Следить за логами worker
docker-compose logs -f worker

# Проверять статус задачи
watch -n 2 'curl -s http://localhost:8000/api/jobs/$JOB_ID | jq'
```

## 🔍 Проверка базы данных

```bash
# Подключиться к PostgreSQL
docker-compose exec postgres psql -U scraper -d scraper

# Проверить таблицы
\dt

# Посмотреть задачи
SELECT id, domain, status, created_at FROM jobs ORDER BY created_at DESC LIMIT 5;

# Посмотреть результаты страниц
SELECT url, http_status, title FROM pages WHERE job_id = '<job_id>' LIMIT 10;

# Выход
\q
```

## 🔍 Проверка Redis очереди

```bash
# Подключиться к Redis
docker-compose exec redis redis-cli

# Проверить очередь
LLEN rq:queue:scraping

# Посмотреть ключи
KEYS rq:*

# Выход
exit
```

## 📊 Проверка экспортов

После завершения задачи:

```bash
# Получить список экспортов
curl http://localhost:8000/api/jobs/$JOB_ID/exports

# Проверить файлы локально
ls -la data/jobs/$JOB_ID/
```

## ❌ Устранение проблем

### API не запускается

```bash
# Проверить логи
docker-compose logs api

# Проверить порт
lsof -i :8000

# Пересоздать контейнер
docker-compose up -d --force-recreate api
```

### Worker не обрабатывает задачи

```bash
# Проверить логи worker
docker-compose logs worker

# Проверить подключение к Redis
docker-compose exec worker redis-cli -h redis ping

# Пересоздать worker
docker-compose up -d --force-recreate worker
```

### Ошибки базы данных

```bash
# Проверить логи PostgreSQL
docker-compose logs postgres

# Пересоздать БД
docker-compose down -v
docker-compose up -d postgres
make migrate
```

### Ошибки импортов

Если видите ошибки типа `ModuleNotFoundError`:

```bash
# Проверить PYTHONPATH
cd network/NEW_PROJECT
export PYTHONPATH=/Users/vostos/Dev/Webscraper:$PYTHONPATH

# Или в Docker пересобрать образ
docker-compose build --no-cache
```

## ✅ Чек-лист успешного тестирования

- [ ] Миграции применились без ошибок
- [ ] API запускается и отвечает на `/api/health`
- [ ] Worker подключается к Redis
- [ ] Можно создать задачу через API
- [ ] Worker обрабатывает задачу
- [ ] Статус задачи обновляется в БД
- [ ] Генерируются экспорты (full.csv, seo.csv)
- [ ] Логи не содержат критических ошибок

## 📝 Следующие шаги

После успешного тестирования:

1. Изучить [README.md](./README.md) для подробных инструкций
2. Ознакомиться с [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)
3. Настроить production переменные окружения
4. Добавить мониторинг и алерты
5. Настроить CI/CD pipeline