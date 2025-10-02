# Docker запуск для NEW_PROJECT

Этот файл описывает как вынести содержимое `network/NEW_PROJECT` в отдельный репозиторий и запускать сервисы в Docker.

## 1. Подготовка отдельного репозитория

1. Создайте пустой репозиторий `git@github.com:Vostos007/ecommerce-scraper-2.0.git` (уже создан в GitHub).
2. Скопируйте только директорию `network/NEW_PROJECT` в рабочую папку будущего репозитория:
   ```bash
   mkdir -p ~/Dev/ecommerce-scraper-2.0
   rsync -a --delete ~/Dev/Webscraper/network/NEW_PROJECT/ ~/Dev/ecommerce-scraper-2.0/
   ```
3. Инициализируйте Git и привяжите origin:
   ```bash
   cd ~/Dev/ecommerce-scraper-2.0
   git init
   git remote add origin git@github.com:Vostos007/ecommerce-scraper-2.0.git
   git add .
   git commit -m "feat: bootstrap scraper backend"
   git push -u origin main
   ```

Дальше обновления можно подтягивать тем же `rsync` (или `git subtree split`) и делать новые коммиты.

## 2. Структура контейнера

- `Dockerfile` — одиночный образ с Python 3.11
- Внутри ставятся зависимости API (`services/api/requirements.txt`) и worker (`services/worker/requirements.txt`)
- Playwright браузер Chromium скачивается на этапе сборки, поэтому worker готов к работе сразу после запуска контейнера

Образ собирает все необходимые пакеты из подпапок `database`, `services`, `config`. Логи, тесты и документация не попадают в образ благодаря `.dockerignore`.

## 3. Переменные окружения

Основные ключи, которые нужно пробросить в контейнер:

| Переменная | Назначение | Значение по умолчанию |
|------------|------------|------------------------|
| `DATABASE_URL` | строка подключения к PostgreSQL | `postgresql://scraper:scraper@localhost:5432/scraper` |
| `REDIS_URL` | URL Redis для RQ очереди | `redis://localhost:6379/0` |
| `CORS_ORIGINS` | список разрешённых Origin (через запятую) | `http://localhost:3000` |
| `ADMIN_TOKEN` | токен для административных запросов | `dev-admin-token` |
| `FLARESOLVERR_URL` | endpoint FlareSolverr | `http://localhost:8191` |
| S3-параметры | `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | см. `services/api/config.py` |

Можно создать `.env` в корне репозитория — Pydantic загрузит его автоматически.

## 4. Сборка образа

```bash
# из корня репозитория ecommerce-scraper-2.0
docker build -t ecommerce-scraper-api:latest .
```

Если нужен лёгкий rebuild после правок кода, используйте более конкретный тег, например `:dev-$(date +%Y%m%d)`.

## 5. Запуск FastAPI

```bash
docker run --rm \
  -p 8000:8000 \
  -e DATABASE_URL=postgresql://scraper:scraper@host.docker.internal:5432/scraper \
  -e REDIS_URL=redis://host.docker.internal:6379/0 \
  --name ecommerce-api \
  ecommerce-scraper-api:latest
```

FastAPI поднимется на `http://localhost:8000`; Swagger доступен по `http://localhost:8000/api/docs`.

## 6. Запуск worker внутри того же образа

```bash
docker run --rm \
  -e DATABASE_URL=postgresql://scraper:scraper@host.docker.internal:5432/scraper \
  -e REDIS_URL=redis://host.docker.internal:6379/0 \
  --name ecommerce-worker \
  ecommerce-scraper-api:latest \
  python -m services.worker.worker
```

> ℹ️  Worker на текущей кодовой базе зависит от модулей `core.*` и `utils.*`. Если вы переносите только поддиректорию `network/NEW_PROJECT`, добавьте эти модули в новый репозиторий (или опубликуйте их как отдельный пакет) прежде чем запускать контейнер с worker-ролями.

## 7. Docker Compose (опционально)

Минимальный `docker-compose.yml` для нового репозитория:

```yaml
env_file: .env
services:
  api:
    build: .
    image: ecommerce-scraper-api:latest
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL}
    depends_on:
      - redis
      - postgres
  worker:
    image: ecommerce-scraper-api:latest
    command: python -m services.worker.worker
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL}
    depends_on:
      - redis
      - postgres
  redis:
    image: redis:7-alpine
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: scraper
      POSTGRES_USER: scraper
      POSTGRES_PASSWORD: scraper
```

## 8. Обновление образа после пуша

1. В репозитории `ecommerce-scraper-2.0` выполните `git pull` (или `git fetch && git checkout tag`), чтобы получить последние изменения.
2. Соберите новый билд: `docker build -t ecommerce-scraper-api:latest .`
3. Перезапустите контейнеры (`docker compose up -d --build` либо `docker stop && docker run ...`).

Для деплоя на сервере можно настроить GitHub Actions, которые после `git push` будут выполнять `docker build` и `docker push` в ваш registry, после чего на сервере достаточно `docker pull` и перезапуска.

## 9. Чистка после сборок

```bash
docker image prune -f
docker builder prune -f
```

Это удалит dangling-слои и сэкономит место, если делаете много rebuild.

---

С этим набором файлов (`Dockerfile`, `.dockerignore`, текущее содержимое `services`/`database`/`config`) проект можно отделить и вести независимо в `ecommerce-scraper-2.0`.
