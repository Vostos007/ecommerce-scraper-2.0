# Installation Guide

Общие требования:

- Python 3.9+ (рекомендовано 3.11)
- Node.js 20+
- Docker (опционально, для полного стека)
- 4 GB RAM минимум (8 GB для комфортной работы)

## 1. Клонирование и подготовка Python

```bash
git clone <repo-url>
cd Webscraper
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt  # по необходимости
```

## 2. Установка Node/PNPM для Dashboard

```bash
cd apps/dashboard
pnpm install --no-frozen-lockfile
cd ../../
```

## 3. Конфигурация окружения

- Скопируйте `.env.example` в `.env` и заполните чувствительные значения.
- Убедитесь, что `NEXT_PUBLIC_APP_URL`, `PORT`, `PYTHON_BIN` синхронизированы (см. playbook).
- Для Docker используйте `docker-compose.yml` или `docker-compose.prod.yml`.

## 4. Проверка конфигураций

```bash
python scripts/validate_config.py
python scripts/validate_imports.py
```

## 5. Запуск основных компонентов

- CLI-скраперы: `python main.py --url <site> --email you@example.com`
- Dashboard: `make dashboard-dev PORT=3050 PYTHON_BIN=$(which python3)`
- Проксисервис: `docker compose --profile proxies up`

## 6. Первичная диагностика

```bash
make baseline  # собирает метрики (время, память, TODO)
pytest -q      # unit и golden тесты
```

## 7. Дополнительные зависимости

- ML (опционально): `numpy`, `pandas`, `matplotlib` (см. ML roadmap).
- Playwright: `pnpm exec playwright install` внутри `apps/dashboard`.

## 8. Откат и очистка

- Удалить Node зависимости: `make dashboard-clean`
- Очистить baseline артефакты: `rm docs/architecture/importtime* docs/architecture/startup-memory.json`
