# 📚 Архитектурный обзор и эволюция Webscraper (октябрь 2025)

## 1. Контекст и цель документа
- Закрепить подробную картину текущего состояния платформы конкурентного мониторинга, включая Python-бэкенд, антибот-инфраструктуру, прокси-оркестрацию, Next.js dashboard и вспомогательные скрипты.
- Зафиксировать сильные стороны и ограничения, отмеченные в `docs/architecture/stabilization-playbook.md`, `docs/architecture/scripts-audit.md` и backlog ARCH-031…039.
- Предложить целевой архитектурный путь на 2025 г. с учётом современных практик CLI, orchestration и AI-assisted scraping.

## 2. Инвентаризация репозитория
### 2.1 Карта верхнего уровня
- `core/` — бэкенд-оркестрация: `core/scraper_engine.py`, `core/proxy_rotator.py`, `core/hybrid_engine.py`, DI (`core/di/container.py`), антибот, scheduler, selector memory.
- `network/` — HTTP/2 и Firecrawl клиенты (`network/httpx_scraper.py`, `network/fast_scraper.py`, `network/firecrawl_client.py`).
- `parsers/` — product/variation parser stack (`parsers/product_parser.py`, `parsers/variation/api.py`, `parsers/variation/impl/legacy.py`).
- `scripts/` — 40+ CLI утилит: fast exporters, batch runner’ы, диагностика, shell-скрипты.
- `apps/dashboard/` — Next.js 15.5.4 + React 19 UI (описание в `apps/dashboard/README.md`).
- `config/` — `settings.json`, proxy policy (`config/proxy/*.yml`), `sites.json` с domain overrides.
- `data/` — runtime артефакты: exports, selector memory, sessions, logs.
- `database/` — SQLite слой, миграции, менеджер (`database/manager.py`).
- `monitoring/` — Prometheus/Grafana конфигурации, `monitoring/stock_monitor.py`.
- `tests/` — Pytest suite (70+ модулей, включая `tests/test_proxy_automation.py`).
- `docs/` — архитектурные playbook’и, API reference, ML roadmap, stabilization журналы.

### 2.2 Core слой
- `core/scraper_engine.py` — конструктор ScraperEngine: загрузка `config/settings.json`, инициализация HTTPX, Playwright, Firecrawl, AntibotManager, BatchProcessor и DatabaseManager; поддержка progress callback’ов и гибридного режима.
- `core/proxy_policy_manager.py` — traffic budgets, circuit breaker, residential burst контроллер, загрузка YAML-политик `config/proxy/global.yml` и site profiles.
- `core/proxy_health_checker.py`, `core/premium_proxy_manager.py` — health-check фреймворк, premium API интеграции (Proxy6, proxy_seller) и авто-покупка.
- `core/site_scheduler.py`, `core/scheduler.py` — cron-планировщик, site-level overrides из `config/sites.json`.
- `core/di/container.py` — лёгкий DI для DB сервисов.
- `core/selector_memory.py`, `core/dynamic_variation_handler.py` — адаптивный selector storage и гибридная обработка вариаций.

### 2.3 Network слой
- `network/httpx_scraper.py` — async HTTPX scraper с metrics, proxy policy, UA rotation, интеграцией `core.antibot_manager`. Поддерживает HTTP/2, streaming, fallback цепочку (direct → datacenter → antibot → flaresolverr → residential).
- `network/fast_scraper.py` — aiohttp + curl_cffi гибрид, rate limiter, system_monitor, динамическая настройка concurrency.
- `network/firecrawl_client.py` — интеграция Firecrawl API в виде fallback/augmentation для тяжелых сайтов.

### 2.4 Parsers
- `parsers/product_parser.py` и `parsers/variation_parser.py` — унифицированные entrypoints с CMS-aware логикой.
- `parsers/variation/api.py` + `parsers/variation/impl/legacy.py` — API-слой для variation extraction, поддержка SixWool/Insales и fallback цепочек.

### 2.5 Скрипты
- Fast exporters (`scripts/ili_ili_fast_export.py`, `scripts/atmosphere_fast_export.py`, `scripts/mpyarn_fast_export.py`, …) — каждый содержит собственный CLI через argparse, повторяет 60–80% логики `scripts/fast_export_base.py`.
- Batch runner’ы (`scripts/run_ili_ili_batches.py`, `scripts/run_mpyarn_batches.py`, `scripts/run_ili_ili_parallel.py`) — обогнали `scripts/site_runner.py` и дублируют функциональность адаптивного concurrency.
- Инфраструктурные (`scripts/fast_export_base.py`, `scripts/baseline.py`, `scripts/profile_mem_startup.py`, `scripts/proxy_stats_export.py`, shell-скрипты для docker/flaresolverr).
- Диагностика/тестовые (`scripts/test_6wool_variations.py`, `scripts/validate_config.py`, `scripts/test_proxy_reachability.sh`).

### 2.6 Frontend
- Next.js App Router (`apps/dashboard/app/`), Zustand/TanStack Query state, SSE лог-стримы (`apps/dashboard/lib/api/export-stream.ts`), API endpoints (`apps/dashboard/app/api/*`), инспектор экспортов и proxy health.
- Документация (`apps/dashboard/docs/qa/frontend/playwright-export.md`, `apps/dashboard/docs/qa/backend/export-status.md`).

### 2.7 Конфигурация и данные
- `config/settings.json` — монолитный JSON (2400+ строк) с HTTPX, FlareSolverr, export logging, observability настройками.
- `config/sites.json` — per-domain overrides: product patterns, pagination, cms detection, antibot сценарии.
- `config/proxy/proxy_pools.yml`, `config/proxy/site_profiles/*.yml` — бюджеты, rotation, burst правила, sequence fallback.
- `data/sites/<domain>/` — sitemap, exports, temp partials, cache.

### 2.8 Monitoring & Ops
- `monitor.py` CLI — отчёт по бюджетам и health-check прокси.
- `monitoring/prometheus.yml`, `monitoring/grafana-dashboards/scraper-overview.json` — базовая observability.
- Make targets: `make baseline`, `make monitor-traffic`, `make proxy-test`.

### 2.9 Tests & QA
- Pytest suites интенсивно покрывают антибот, fast exporters, proxy automation, database (см. `tests/test_proxy_automation.py`, `tests/test_fast_export_base.py`, `tests/test_site_scheduler.py`).
- Golden tests для variation parser (`tests/parsers/golden/`).
- QA артефакты в `tests/pytest-triage-2025-09-29.md`.

### 2.10 Документация и процессы
- Архитектурные playbook’и в `docs/architecture/` (stabilization, refactor plan parser/db, scripts audit, security monitoring).
- API reference, guides, ML roadmap.
- `AGENTS.md` + `.agents/` — роли для специализированных обзоров.

## 3. Архитектурные подсистемы (текущее состояние)
1. **Scraping Orchestration**: `ScraperEngine` + `HybridScrapingEngine` обеспечивают переключение между HTTPX, Playwright, Firecrawl. BatchProcessor управляет батчами, прогресс callbacks.
2. **Proxy & Antibot Stack**: Policy manager, ProxyRotator, PremiumProxyManager, ContentValidator и SessionManager обеспечивают budgets, health-check, сессии (`core/session_manager.py`).
3. **Scheduling & Automation**: `run_sites.py` + `core/site_scheduler.py` + `data/scheduled_tasks.json` orchestrate cron/parallel execution, adaptive concurrency (`AdaptiveConcurrencyController`).
4. **Data Persistence**: SQLite (database/manager.py) + migration manager, history writer, export writers (`utils/export_writers.py`).
5. **Frontend & Control Plane**: Next.js dashboard запускает Python скрипты, агрегирует метрики, хранит config UI.
6. **Observability & Tooling**: Prometheus config, Makefile задачи, docs with baseline metrics.

## 4. Сильные стороны
- Богатый антибот и proxy-менеджмент (budget контроллеры, авто-покупка premium, burst логика).
- Гибридный scraping engine с fallback-цепочкой и Firecrawl интеграцией.
- Широкое тестовое покрытие, особенно для exporters и variation parser.
- Документированные playbook’и и baseline процессы.
- Dashboard с SSE логами, API reference, командный UI для запуска скриптов.

## 5. Ограничения и технический долг
- 47 CLI-скриптов с дублирующими `argparse` секциями (`scripts/ili_ili_fast_export.py`, `scripts/atmosphere_fast_export.py`, `scripts/triskeli_fast_export.py`) — подтверждено в `docs/architecture/scripts-audit.md`.
- Batch runner’ы и diagnostic scripts смешаны в `scripts/`, отсутствует единый entrypoint.
- `config/settings.json` перегружен: смешивает HTTPX, logging, UI темы, incremental exports; нет формальной схемы/валидации.
- Proxy конфигурация распределена между JSON/YAML без централизованного registry.
- Отсутствует единая очередь задач; `run_sites.py` и Make targets полагаются на локальный threading.
- Наблюдаемость: Prometheus присутствует, но метрики не унифицированы с CLI логикой; structured logging частично реализован в `utils/logger.py`.
- Frontend зависит от конкретных скриптов по имени, нет слоя абстракции над exporters.

## 6. Документация и процессы
- Stabilization playbook фиксирует метрики cold start, RSS, TODO/FIXME.
- Scripts audit (ARCH-035) уже рекомендует unified CLI и удаление устаревших скриптов.
- Pytest triage требует запусков батчами ≤ 1 часа.
- Приняты стандарты PEP 8, black/isort, semantic commit формат.

## 7. Целевое архитектурное видение 2025
### 7.1 Unified CLI с plugin Registry
- Создать единый `scraper.py` (Typer) с подкомандами `export`, `run`, `health`, `config`, `legacy`.
- Регистрация domain profiles через декоратор/registry (`export_profile("ili-ili.com")`) и auto-discovery модулей внутри `core/export_profiles/`.
- Использовать Typer sub-typer pattern (`Typer.add_typer`) для группировки команд и вложенных CLI, поддерживая rich help панели и Annotated types для строгой валидации [Typer Docs][ref-typer].
- Legacy CLI shim: `scripts/legacy/<name>.py` перенаправляет в `scraper.py` с DeprecationWarning, обеспечивая совместимость существующих cron/job runner’ов.

### 7.2 Configuration-as-Code
- Ввести `config/domains/<domain>.yml` с декларативной схемой (pydantic). Генерация схемы и валидация в CI, синхронизация с `config/sites.json`.
- `config/proxy/` преобразовать в модульную структуру: global defaults + domain overrides + connection budgets в едином YAML.
- Добавить tooling: `scraper.py config check` и `scraper.py config diff`.

### 7.3 Оркестрация задач и Worker Pool
- Выделить job queue (Celery/RQ) для экспорта: Typer CLI отправляет задания в Redis, worker’ы используют существующий ScraperEngine.
- `run_sites.py` преобразовать в orchestrator, формирующий job batch и отслеживающий статус через backend storage.
- Использовать async crawling best practices из Firecrawl (`start_crawl`, incremental save) для построения мониторинга очереди и паузы/возобновления [Firecrawl Crawl Guide][ref-firecrawl].

### 7.4 Proxy & AI Operations Evolution
- Формализовать AI-driven proxy selection: агрегировать метрики из PremiumProxyManager + ProxyHealthChecker и добавлять ML эвристику для выбора пула, опираясь на индустриальный тренд AI web scraping (automated proxy/browser management, compliance) [Zyte AI Scraping 2025][ref-zyte].
- План интеграции ML моделей/feedback loop (residential burst, ban prediction) с возможностью дозавоза AI Optimizer на Phase 3.

### 7.5 Observability-first Design
- Unified structured logging (JSON) с correlation ID, domain, proxy strategy.
- Prometheus exporter для CLI (Typer callback регистрирует metrics сервер), вывод в Grafana dashboards.
- Расширить `monitor.py`/`scraper.py health` для мгновенного snapshot budgets + proxy health.

### 7.6 Frontend Alignment
- Dashboard обращается к единому CLI API (`scraper.py export <domain>`). Backend Next.js вызывает новый CLI с тонким адаптером.
- Документация `docs/scripts_reference.md` описывает единый CLI, legacy команды и roadmap.

### 7.7 Inspiration из современной CLI архитектуры
- Опора на модульный CLI слой, аналогичный Gemini CLI (entry, core engine, tool registry) для структурирования подкоманд и расширений [Gemini CLI Architecture][ref-gemini].

## 8. Фазовый план внедрения
| Фаза | Срок | Основные задачи | Артефакты |
|------|------|-----------------|-----------|
| **Phase 1: Унитаризация CLI (1–2 недели)** | октябрь 2025 | `scraper.py` (Typer), registry доменов, `config/domains/*.yml`, Pydantic валидация, structured logging MVP | Новый CLI, schema checker, обновлённые docs (`docs/scripts_reference.md`) |
| **Phase 2: Plugin миграция и реорганизация (3–5 недель)** | ноябрь 2025 | Перенос fast exporters в `core/export_profiles/`, реорганизация `scripts/` (`scripts/legacy/` + `tools/diagnostics/`), Observability (Prometheus metrics, monitor.py расширение), simple circuit breaker telemetry | Обновлённые тесты (`tests/scripts/test_cli_matrix.py`), Grafana dashboard v2 |
| **Phase 3: Queue & AI enhancements (2–3 месяца)** | декабрь 2025 – январь 2026 | Redis/Celery job queue, worker pool, AI proxy heuristics (feedback loop), Dashboard интеграция с job API, подготовка ML экспериментальной среды | Queue service, metrics pipeline, AI proxy report |

## 9. Риски и митигация
- **Backward compatibility**: Legacy shim + предупреждения + документированная матрица соответствий.
- **Объём работ**: деление на фазы, мердж по доменам, контроль тестового покрытия.
- **Observability gap**: внедрить smoke-тест (`scraper.py self-check`) и автоматизированную проверку загрузки профилей.
- **Proxy budgets**: мониторить лимиты при рефакторинге конфигураций, хранить snapshot деривов.
- **AI Integrations cost**: запустить пилот на одном домене, измерить ROI перед масштабированием.

## 10. Индикаторы успеха
- Количество активных скриптов ≤ 32 (−32% от текущих 47).
- Unified CLI покрывает ≥ 90% use cases dashboard и ops.
- Время on-call triage снижается за счёт структурированных логов/metrics (метрика — среднее время до диагностики прокси).
- Покрытие тестами ключевых команд CLI ≥ 80%.
- Proxy failure rate снижается благодаря дневному бюджету/AI эвристикам.

## 11. Источники
- [Typer CLI & plugin patterns][ref-typer]
- [AI Web Scraping as the Future of Scalable Data Collection][ref-zyte]
- [Mastering Firecrawl’s Crawl Endpoint (async orchestration)][ref-firecrawl]
- [Gemini CLI Project Architecture Analysis (modular CLI tooling)][ref-gemini]

[ref-typer]: https://typer.tiangolo.com/tutorial/subcommands/nested-subcommands/
[ref-zyte]: https://www.zyte.com/blog/ai-web-scraping-as-the-future-of-scalable-data-collection/
[ref-firecrawl]: https://www.firecrawl.dev/blog/mastering-the-crawl-endpoint-in-firecrawl
[ref-gemini]: https://aicodingtools.blog/en/gemini-cli/architecture-analysis

## 12. Migration map (scripts → unified CLI)
| Старый скрипт                    | Новая команда (`scraper.py`)   | Статус | Дата деприкации |
|----------------------------------|--------------------------------|--------|------------|
| scripts/ili_ili_fast_export.py   | export --site ili-ili.com      | READY  | 2025-11-15 |
| scripts/atmosphere_fast_export.py| export --site atmospherestore.ru | WIP   | 2025-12-01 |
| scripts/run_ili_ili_batches.py   | run --site ili-ili.com --mode batch | PLAN | 2025-12-15 |
| scripts/proxy_stats_export.py    | health proxies export          | READY  | 2025-11-15 |
| scripts/site_runner.py           | run --catalog <file>           | WIP    | 2026-01-15 |
| ...                              | ...                            | ...    | ...        |

## 13. RACI
| Задача                        | R (Responsible) | A (Accountable) | C (Consulted) | I (Informed) |
|------------------------------|-----------------|-----------------|---------------|--------------|
| Unified CLI rollout          | Backend Lead    | CTO             | Frontend Lead | QA, DevOps   |
| Proxy health monitoring      | SRE             | CTO             | Backend Team  | PM           |
| Dashboard integration        | Frontend Lead   | CTO             | Backend Lead  | QA           |
| Reports pipeline             | Data Analyst    | CTO             | Backend       | PM, Ops      |

## 14. Backout plan
- **Критерии**: падение успешных job > 5% за сутки, рост 5xx API > 2×, превышение бюджетов > 20%.
- **Действия**: переключить feature-flag `USE_LEGACY_CLI=true`, вернуть cron на `scripts/*`, откатить релиз, уведомить #incidents.
- **Данные**: миграции обратимы (alembic downgrade); артефакты остаются.
- **Отчетность**: post-mortem, фиксация `policy_rollback_from`/`to` в логах.
