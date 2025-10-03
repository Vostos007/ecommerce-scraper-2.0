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
