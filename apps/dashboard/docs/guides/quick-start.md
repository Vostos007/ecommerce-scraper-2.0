# Quick Start Guide

## 1. Запуск первого экспорта

```bash
source .venv/bin/activate
python main.py --url https://6wool.ru --email reports@example.com \
  --max-products 500 --export-format xlsx
```

- Результат появится в `data/sites/6wool.ru/exports/latest.xlsx`.
- Логи скрапинга доступны в `logs/` и через Dashboard (см. ниже).

## 2. Просмотр Dashboard

```bash
make dashboard-dev PORT=3050 PYTHON_BIN=$(which python3)
# UI будет доступен на http://localhost:3050/dashboard
```

Основные действия на дэшборде:

1. Выбрать площадку → нажать "Запустить экспорт".
2. Отследить логи SSE внизу страницы.
3. Скачать последний Excel-файл.

## 3. Быстрые метрики

```bash
make baseline
```

Артефакты сохраняются в `docs/architecture/`. Сравнивайте значения до/после изменений.

## 4. Проверка golden-тестов

```bash
pytest -m "golden or critical" -q
```

Тесты «golden» гарантируют, что рефакторинг парсеров не ломает ключевые домены.

## 5. Подготовка ML-компонентов (статус: заглушка)

- Классы `DemandPredictor` и `AnomalyDetector` возвращают статические значения.
- Для включения реальной логики потребуется ML roadmap (см. `docs/architecture/async-migration-roadmap.md`).
- Пока держите опцию отключённой через конфиг `monitoring/settings.json`.

## 6. Теленаблюдение

После `make baseline` изучите:

- `docs/architecture/importtime-summary.csv` — тяжелые импорты.
- `docs/architecture/startup-memory.json` — RSS после импорта.
- `logs/` — наличие 500/traceback.

## 7. Типичные проблемы

- **Блокировки сайтом**: настроить прокси в `config/settings.json` или задействовать Docker профиль `proxies`.
- **Медленный старт (>1s)**: убедиться, что ленивые импорты выполнены (см. стабилизационный план).
- **Ошибки UI fetch**: проверить `resolveApiOrigin` и переменные окружения.
