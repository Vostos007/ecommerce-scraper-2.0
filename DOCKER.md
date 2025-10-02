# Docker-инфраструктура ecommerce-scraper-2.0

В репозитории собраны все компоненты проекта: FastAPI backend, RQ worker, Next.js Dashboard, а также вспомогательные сервисы (PostgreSQL, Redis, MinIO, FlareSolverr). Ниже — единый сценарий запуска и обновления.

## Структура образов

| Dockerfile               | Назначение                              |
|--------------------------|-----------------------------------------|
| `Dockerfile`             | Playwright Python образ с API и worker  |
| `Dockerfile.dashboard`   | Next.js Dashboard (production build)    |

Оба Dockerfile копируют каталоги `core/`, `parsers/`, `scripts/`, `utils/`, `services/`, `database/`, `apps/dashboard/`, поэтому внутри контейнеров есть весь код.

## Быстрый запуск (всё сразу)

```bash
# В первый раз (или после изменений) соберите образы
docker compose build

# Запускаем весь стек
docker compose up -d
```

После старта доступны:

- Dashboard: `http://localhost:3000` (UI для запуска экспортов)
- API Swagger: `http://localhost:8000/api/docs`
- MinIO console: `http://localhost:9001`

Основные сервисы:

- `api` — FastAPI (uvicorn)
- `worker` — RQ worker (использует тот же образ, что и API)
- `dashboard` — Next.js фронтенд
- `postgres`, `redis`, `minio`, `flaresolverr` — инфраструктура
- `minio-init` — однократная инициализация bucket-а `scraper-artifacts`

Экспортируемые файлы сохраняются во volume `exports_data` (см. `docker-compose.yml`).

Просмотр логов:

```bash
docker compose logs -f api
# или
docker compose logs -f worker
```

Остановка:

```bash
docker compose down
```

## Переменные окружения

Часть значений уже зашита в `docker-compose.yml`. При необходимости переопределите их в `.env` рядом с compose-файлом — Docker Compose автоматически подхватит.

| Переменная      | Назначение                                    | По умолчанию                     |
|-----------------|------------------------------------------------|----------------------------------|
| `DATABASE_URL`  | Строка подключения к PostgreSQL               | `postgresql://scraper:...`       |
| `REDIS_URL`     | Адрес Redis                                   | `redis://redis:6379/0`           |
| `FLARESOLVERR_URL` | Endpoint FlareSolverr                      | `http://flaresolverr:8191`       |
| `S3_ENDPOINT` / `S3_*` | Настройки MinIO                         | `http://minio:9000`, `minioadmin`|
| `CORS_ORIGINS`  | Допустимые origin'ы для API                   | `http://localhost:3000,...`      |
| `NEXT_PUBLIC_API_BASE_URL` | Базовый URL API для фронтенда       | `http://api:8000` (внутри compose)|

## Обновление после новых коммитов

```bash
git pull
# перезагружаем сервисы (соберёт только изменившиеся слои)
docker compose up -d --build
```

Для серверного деплоя можно настроить CI (например, GitHub Actions), который будет собирать и пушить образы `ecommerce-scraper/api:latest` и `ecommerce-scraper/dashboard:latest` в реестр. Тогда на сервере достаточно `docker compose pull && docker compose up -d`.

## Локальные проверки перед сборкой

### Python (API + worker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r services/api/requirements.txt -r services/worker/requirements.txt
pytest
```

### Dashboard

```bash
cd apps/dashboard
corepack enable pnpm
pnpm install
pnpm lint && pnpm test
pnpm build
```

## Очистка

```bash
docker compose down -v   # удалить контейнеры и volumes
docker image prune -f
```

Теперь одни команды `docker compose build` и `docker compose up -d` поднимают полный сервис. Обновление — `git pull && docker compose up -d --build`.
