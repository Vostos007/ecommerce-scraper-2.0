## 2025-10-03 — Proxy dashboard metrics
- Роль: frontend-developer (primary), backend-architect (secondary)
- Действия: добавил сборщик метрик `proxy_stats.collector`, написал unit-тест; переписал `scripts/proxy_stats_export.py` на использование агрегатора; проверил вывод и тесты.
- Результат: `python scripts/proxy_stats_export.py` возвращает значения, основанные на отчётах и логах; тесты `pytest network/NEW_PROJECT/tests/test_proxy_stats.py` проходят.
- TODO: докрутить вычисление стран/стоимости для премиум-прокси по реальным данным, подключить API к dashboard SSE при появлении стрима.
## 2025-10-03 — Dashboard summary & version badge
- Роль: frontend-developer (primary), backend-architect (secondary)
- Действия: дополнил `/api/summary` фильтрами по электронным товарам и завышенным остаткам, добавил unit-тест; заменил расчёт `/api/proxy/stats` на новый Node-агрегатор `lib/proxy-stats` без спавна python; вывел шильдик версии в `TopNav`.
- Результат: метрики сводки не учитывают электронные позиции и аномальные остатки, прокси-дашборд опирается на актуальные артефакты, фронт отображает версию приложения вместо DEMO mode.
- TODO: дополнить агрегатор обработкой premium источников при появлении реальных API и синхронизацией с python-CLI.
## 2025-10-03 — Repository guidelines refresh
- Роль: docs-architect (primary), context-manager (secondary)
- Действия: пересмотрел PRD, архитектуру и политики; собрал сведения о директориях `network/NEW_PROJECT`, `services/api`, `apps/dashboard`; подготовил документ `network/NEW_PROJECT/AGENTS.md` с правилами ведения разработки.
- Результат: новая инструкция "Repository Guidelines" описывает структуру проекта, команды сборки/тестов, регламент коммитов и правила безопасности.
- TODO: синхронизировать документ с будущей автоматизацией bulk-runner, когда API и миграции будут добавлены.
## 2025-10-03 — Добавлен экспортёр manefa.ru
- Роль: backend-developer (primary), frontend-developer (secondary)
- Действия: подключил новый скрипт `manefa_fast_export.py`, расширил allowlist `SCRIPT_ALLOWLIST`, пересобрал API/worker образы.
- Результат: площадка manefa.ru поддерживается в частичном и массовом парсинге; после первой выгрузки появится в дашборде.
## 2025-10-03 — Manefa exporter python alignment
- Роль: backend-architect (primary), docs-architect (secondary)
- Действия: перезапустил dashboard dev на порте 3002 с PYTHON_BIN=/Users/vostos/miniconda3/bin/python; обновил `scripts/manefa_fast_export.py` под новый `fast_export_base` (новые аргументы antibot, прогресс, resume), проверил запуск `python -m scripts.manefa_fast_export --limit 2`.
- Результат: экспортер больше не падает на Python 3.9/argparse, работает в окружении Python 3.13, прогресс и антибот синхронизированы с базовыми утилитами.
- TODO: восстановить доступность домена manefa.ru в тестовом окружении (сейчас resolve возвращает `nodename nor servname`).
## 2025-10-03 — AGENT guide wording cleanup
- Роль: docs-architect (primary)
- Действия: обновил `AGENTS.md`, упростил инструкцию про оформление патчей, убрал упоминание о просмотрах "proposed changes".
- Результат: документ теперь требует оформлять изменения как завершённые патчи с тестами без лишних указаний про отображение правок пользователю.
## 2025-10-04 — Manefa redirect handling
- Роль: backend-architect (primary)
- Действия: обновил `scripts/manefa_fast_export.py` — нормализую product URL без хвостового `/`, включил follow_redirects и сохраняю финальный URL; проверил запуск `python -m scripts.manefa_fast_export --limit 1` (доменные 301 решаются, но DNS до manefa.ru в dev всё ещё нестабилен).
- Результат: скрипт корректно формирует канонические ссылки, 301 больше не помечаются как ошибки; при доступном DNS прогон завершится без массовых fail.
- TODO: восстановить резолв manefa.ru или добавить локальный override, затем повторить полный прогон.
## 2025-10-04 — Manefa variations parsing
- Роль: backend-architect (primary)
- Действия: переработал `scripts/manefa_fast_export.py` — добавил извлечение вариантов из payload и DOM (`data-variants`, `data-variant-id`), сохранил итоговый URL после редиректов, подготовил отладочный режим `MANEFA_DEBUG_PAYLOADS`.
- Результат: экспорт теперь формирует список вариаций (цветов) с количеством и атрибутами, что позволяет выгрузке строить отдельные строки по каждой вариации.
- TODO: после стабилизации DNS повторно прогнать полный экспорт и убедиться, что `variations` заполнены.
- Версия: pyproject.toml → 0.1.1, apps/dashboard/package.json → 0.1.1
- ExportForm по умолчанию запускает без resume (теперь чекбокс выключен).
## 2025-10-04 — Манефа вариации в CSV/Excel
- Роль: backend-architect (primary)
- Действия: переработал `scripts/manefa_fast_export.py`, чтобы нормализовать вариации (id, sku, тип, цена, остаток, атрибуты); обновил `utils/export_writers.py`, добавив строки на каждую вариацию и новые столбцы; поднял версии до 0.1.3.
- Результат: `full.csv`/`xlsx` содержат отдельную запись на каждую вариацию товара, Dashboard считывает корректные метрики, выгрузка манефы больше не ограничена 120 строками.
- Обновлены форматы чисел: CSV теперь использует `;` и десятичную запятую, цены/остатки в Excel экспортируются как числа.
- Исправлено отображение версии в TopNav: теперь используется фактический номер из package.json (fallback через env `NEXT_PUBLIC_APP_VERSION`).
