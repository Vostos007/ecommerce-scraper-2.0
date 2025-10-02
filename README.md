# Webscraper Backend Service

FastAPI backend с RQ worker pool для orchestration слоя проекта Webscraper.

## 📁 Структура проекта

```
network/NEW_PROJECT/
├── services/
│   ├── api/              # FastAPI backend
│   │   ├── main.py       # Точка входа приложения
│   │   ├── config.py     # Настройки (pydantic-settings)
│   │   ├── models.py     # Pydantic схемы
│   │   ├── dependencies.py # DI для FastAPI
│   │   ├── queue.py      # RQ интеграция
│   │   └── routes/       # API endpoints
│   │       ├── jobs.py   # Управление задачами
│   │       ├── exports.py # Экспорты
│   │       ├── health.py # Health check
│   │       └── sse.py    # Server-Sent Events
│   └── worker/           # RQ Worker
│       ├── worker.py     # Точка входа worker
│       ├── tasks.py      # Определения задач
│       └── job_executor.py # Оркестратор выполнения
├── database/
│   ├── manager.py        # DatabaseManager (asyncpg)
│   ├── migrate.py        # Migration runner
│   └── migrations/       # SQL миграции
├── Dockerfile.backend    # Docker образ для API+Worker
├── docker-compose.yml    # Все сервисы
├── .env.example          # Пример конфигурации
└── Makefile              # Команды управления
```

## 🚀 Быстрый старт

### 1. Копировать .env

```bash
cd network/NEW_PROJECT
cp .env.example .env
```

### 2. Запустить все сервисы

```bash
make up
```

Это запустит:
- PostgreSQL (порт 5432)
- Redis (порт 6379)
- FlareSolverr (порт 8191)
- MinIO (порты 9000, 9001)
- API (порт 8000)
- 2 Worker процесса

### 3. Применить миграции

```bash
make migrate
```

### 4. Проверить статус

- API: http://localhost:8000
- API Docs: http://localhost:8000/api/docs
- MinIO Console: http://localhost:9001 (admin/minioadmin)

## 📝 Основные команды

```bash
# Управление Docker
make up          # Запустить все сервисы
make down        # Остановить все сервисы
make logs        # Показать логи

# Разработка (локально)
make install     # Установить зависимости
make api         # Запустить API локально
make worker      # Запустить Worker локально

# Утилиты
make migrate     # Применить миграции БД
make test        # Запустить тесты
make clean       # Очистить volumes и temp файлы
```

## 🔧 Локальная разработка

### Запустить API локально

```bash
cd network/NEW_PROJECT
export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"
export REDIS_URL="redis://localhost:6379/0"
python -m uvicorn services.api.main:app --reload --port 8000
```

### Запустить Worker локально

```bash
cd network/NEW_PROJECT
export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"
export REDIS_URL="redis://localhost:6379/0"
python services/worker/worker.py
```

## 📡 API Endpoints

### Jobs

- `POST /api/jobs` - Создать новую задачу скрейпинга
- `GET /api/jobs` - Список задач
- `GET /api/jobs/{job_id}` - Статус задачи
- `POST /api/jobs/{job_id}/cancel` - Отменить задачу

### Exports

- `GET /api/jobs/{job_id}/exports` - Список экспортов задачи

### Monitoring

- `GET /api/health` - Health check
- `GET /api/jobs/{job_id}/stream` - SSE stream прогресса

### Documentation

- `GET /api/docs` - Swagger UI
- `GET /api/redoc` - ReDoc

## 🧪 Тестирование

### Создать тестовую задачу

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "sitemap_urls": ["https://example.com/page1", "https://example.com/page2"],
    "options": {
      "domain": "example.com",
      "max_concurrency": 2
    }
  }'
```

### Проверить статус задачи

```bash
curl http://localhost:8000/api/jobs/{job_id}
```

### Получить экспорты

```bash
curl http://localhost:8000/api/jobs/{job_id}/exports
```

## 🗄️ База данных

### Схема

- `jobs` - Задачи скрейпинга
- `pages` - Результаты страниц
- `snapshots` - Снимки для diff
- `exports` - Артефакты экспорта
- `metrics` - Временные ряды метрик

### Миграции

Миграции находятся в `database/migrations/`:
- `001_create_jobs_schema.sql` - Базовая схема

Для создания новой миграции:
1. Создать файл `00X_description.sql`
2. Запустить `make migrate`

## 🔗 Зависимости

### Core модули (из корня проекта)

- `../../core/` - Scraper engine, parsers
- `../../utils/` - Export writers, helpers
- `../../parsers/` - Site-specific parsers

### Python пакеты

- FastAPI + Uvicorn
- asyncpg (PostgreSQL async)
- Redis + RQ (очередь задач)
- Pydantic Settings
- Playwright (для worker)

## 🐛 Отладка

### Посмотреть логи

```bash
# Все сервисы
make logs

# Конкретный сервис
docker-compose logs -f api
docker-compose logs -f worker
```

### Подключиться к базе

```bash
docker-compose exec postgres psql -U scraper -d scraper
```

### Проверить Redis очередь

```bash
docker-compose exec redis redis-cli
> KEYS *
> LLEN rq:queue:scraping
```

## 📚 Дополнительно

- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) - Детальный план реализации
- [Architecture.md](./Architecture.md) - Архитектура системы
- [prd.md](./prd.md) - Product Requirements
