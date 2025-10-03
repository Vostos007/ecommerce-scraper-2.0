# AGENT_GUIDE.md — Вайбкодинг Webscraper

> Работай **только** внутри `network/NEW_PROJECT/` и строго по текущей документации. Никаких ссылок на внешние подсказки.

## 1. Правила
- Перед любой задачей прочитай `prd.md`, `Architecture.md`, `tech_stack_policy.md`.
- Если нужно API — смотри `docs/API.md`, `docs/openapi.yaml`.
- Схемы БД — `docs/db_schema.md`.
- Отчёты и CSV — `reports_spec.md`, файл `Вайбкодинг Аналитика.xlsx`.
- Инциденты/операции — `RUNBOOKS/`.
- Security/robots — `SECURITY.md`.
- Соответствие политикам — `tech_stack_policy.md`.
- Все настройки/примерные политики — `config/policies.example.yml`.
- Переменные окружения — `.env.example`.

## 2. Ограничения
- Не трогай файлы вне `network/NEW_PROJECT/`.
- Не меняй формат CSV/Excel, схемы API или БД без обновлений документов.
- Не включай residential и не отключай robots без правки профиля и логирования.
- Любые новые политики — фиксируй в `tech_stack_policy.md` и `config/policies*.yml`.

## 3. Последовательность работы
1. Изучи документацию по задаче (см. выше).
2. Используй `RUNBOOKS` для конкретных кейсов (например, `ili-ili.md`, `residential-burst.md`).
3. Перед изменениями проверь diff (`git diff`).
4. Тестируй (`make test`, smoke `npm run scrape ...`).
5. Описывай изменения в README/доках, если меняешь поведение.

## 4. Обновления/деплой
- Обновления через git pull + docker compose (когда появится). Не выдумывай свои процессы.
- Миграции скриптов → обновляй таблицу в `architect-review-2025/current-state-and-future.md`.

Следуя этому файлу, агент не уйдёт «в сторону» и будет работать в согласованном контуре.
## 5. Логирование и навигация
- Рабочие заметки веди в `logs/DEV_LOG.md` (дата, тема, действия, результат, TODO).
- Решения документируй в `logs/DECISIONS.md` (формат mini-ADR: контекст, решение, последствия).
- Перед началом задачи просмотри последние записи, чтобы понимать контекст.

## 6. Ролевой автопилот (`.agents`)
- Запуская агент в автоматическом режиме, сначала активируй `context-manager` для сбора исходного контекста и проверки, нет ли пересечения с текущими задачами.
- Определи тип задачи и назначь профиль из `.agents`:
  - архитектура/бэкенд → `backend-architect`, `backend-security-coder`;
  - базы данных → `database-admin`, `database-optimizer`;
  - деплой/инфраструктура → `deployment-engineer`, `devops-troubleshooter`;
  - фронтенд/интерфейсы → `frontend-developer`, `frontend-security-coder`;
  - документация/обновление процессов → `docs-architect`, `api-documenter`, `dx-optimizer`;
  - тесты и качество → `tdd-orchestrator`, `test-automator`, `code-reviewer`;
  - производительность и наблюдаемость → `performance-engineer`, `observability-engineer`;
  - безопасность и аудит → `security-auditor`.
- Если задача затрагивает несколько доменов, назначь основную роль (primary) и одну вспомогательную (secondary); фиксируй выбор в `logs/DEV_LOG.md` перед стартом.
- После завершения шага агент обновляет контекст: возвращай управление `context-manager`, который решает — продолжать текущую роль или переключаться.
- Любые новые роли добавляй в `.agents` через pull request и обновляй этот раздел.

Когда ты что-то делаешь, всегда думай о том, как это вписывается в общую картину проекта и его долгосрочные цели.

КОгда ты настраиваешь сервер, окружение или зависимости и т.д. - не проси меня что-то делать, делай всё сам. Ты же агент, ты должен уметь всё делать сам. Ты все проверяешь тестируешь запускаешь и уже после этого сообщаешь мне что всё готово и работает. Ты должен уметь всё делать сам. Я уже проверяю результаты твоей работы.

Write code for clarity first. Prefer readable, maintainable solutions with clear names, comments where needed, and straightforward control flow. Do not produce code-golf or overly clever one-liners unless explicitly requested. Use high verbosity for writing code and code tools.

Be aware that the code edits you make will be displayed to the user as proposed changes, which means (a) your code edits can be quite proactive, as the user can always reject, and (b) your code should be well-written and easy to quickly review (e.g., appropriate variable names instead of single letters). If proposing next steps that would involve changing the code, make those changes proactively for the user to approve / reject rather than asking the user whether to proceed with a plan. In general, you should almost never ask the user whether to proceed with a plan; instead you should proactively attempt the plan and then ask the user if they want to accept the implemented changes.

# Массовый прогон всех площадок — план внедрения

## 1. Архитектура и оркестратор
- Добавить сервис `bulk-runner` (можно как модуль в `services/api`): отвечает за планирование массовых прогонов.
- На вход принимает список площадок (по умолчанию все из `config/sites.json`).
- Создаёт запись `bulk_job` (таблица `bulk_jobs`, состояние, timestamps, пользователь).
- Для каждого домена — создаёт `job` (уже существующий механизм) и связывает через `bulk_job_sites` (bulk_job_id, site, job_id, status, processed_count).
- Сохраняет `sitemap_total_urls` и `estimated_duration` (берём из sitemap == количество, fallback: среднее по прошлым job’ам).
- Хранит прогресс в `bulk_job_progress` (timestamp, site, processed_urls, total_urls, duration, status).
- По завершении всех job’ов — инициирует сборку архива с отчетами.

## 2. API изменения (services/api)
- Новый роут `POST /api/bulk-runs` — старт массового прогона; параметры: список сайтов, лимит concurrency, флаги (skip_existing, dry_run).
- `GET /api/bulk-runs/{id}` — агрегированный статус (progress %, ETA, site-wise breakdown).
- `GET /api/bulk-runs/{id}/stream` — SSE с прогрессом (агрегирует сигналы от job’ов).
- `POST /api/bulk-runs/{id}/cancel` — отмена всех job’ов.
- `GET /api/bulk-runs/{id}/archive` — выдаёт ссылку/проксы на архив.
- Из API worker hook слушать события job’ов (использовать RQ сигнал или текущий SSE) и обновлять `bulk_job_progress`.
- Добавить pydantic-схемы и сервис-слой `BulkRunnerService` (создание, обновление, архив).

## 3. Worker изменения
- Worker, получая задание от bulk-runner (обычный job), должен отправлять heartbeat: `progress_hook(site, processed, total)`.
- Расчёт `processed` — из текущего writer (уже есть IncrementalWriter processed_urls).
- Встраиваем `progress_hook` в `fast_export_base.py` (поддержка внешнего callback).
- При завершении job’а — репортим `finished` (success/fail, артефакты).

## 4. Dashboard (apps/dashboard)
- Новый раздел "Массовый прогон": кнопка "Запустить все" (форма с параметрами).
- Карточка состояния: общий прогресс %, оставшееся время (на основе `sum(total_urls)` и текущей скорости).
- Таблица по сайтам (статус, processed/total, ETA, ссылка на живой job).
- SSE от `/api/bulk-runs/{id}/stream` — обновление прогресса в real-time.
- После завершения — кнопка "Скачать архив" + отдельные ссылки по площадкам.
- Доработать существующий DownloadCenter: отображать активный bulk-run, возможность скачивать архив/индивидуальные файлы.

## 5. Хранение и расчёт ETA
- `processed_total = sum(processed_urls)`; `total_urls = sum(total_urls)`.
- Скорость: `processed_total / elapsed_seconds` (обновлять каждые N секунд).
- ETA: `(total_urls - processed_total) / max(speed, epsilon)`.
- Для площадки используем собственную скорость (если есть история) или глобальную.
- Хранить последнюю скорость/ETA в `bulk_job_progress` (для отображения).

## 6. Архивация отчётов
- После завершения всех job’ов — bulk-runner вызывает `archive_exports(bulk_job_id)`.
- Собрать `full.csv`, `seo.csv`, `diff.csv`, `.xlsx` каждого сайта из `data/sites/<domain>/exports` (возможно уже в MinIO).
- Упаковать в `bulk_runs/{bulk_job_id}/reports.zip` (использовать MinIO/S3 через существующий S3 клиента).
- В API выдавать ссылку на скачивание (подписанный URL или прокси через API).

## 7. Docker и зависимости
- Обновить `Dockerfile` (API/worker): убедиться, что Python 3.12 и зависимости соответствуют (pip install -r requirements.txt).
- Обновить `docker-compose.yml`: добавить переменные `BULK_MAX_CONCURRENCY`, `BULK_ARCHIVE_BUCKET`, `BULK_PROGRESS_POLL_INTERVAL`.
- Для dashboard Dockerfile — убедиться, что `PYTHON_BIN` указывает на python3.12 и передавать в контейнер (пример: `ENV PYTHON_BIN=/usr/local/bin/python3.12`).
- В CI (если есть) — запуск `pnpm --dir apps/dashboard test`, `pytest`, `bulk-run smoke` (можно симулировать 2 сайта).

## 8. Миграции и обновление зависимостей
- Создать миграцию для таблиц `bulk_jobs`, `bulk_job_sites`, `bulk_job_progress`.
- При обновлении — выполнить `alembic upgrade head` (или наш миграционный скрипт).
- Добавить требования в `requirements.txt` (если нужны доп. библиотеки: например, `zipstream` или `aioboto3`).
- В dashboard — возможно обновить зависимости (если вводим новые пакеты для SSE/stores).

## 9. Тестирование и наблюдаемость
- Unit-тесты: сервис bulk-runner, API endpoints, progress aggregator.
- Интеграционные тесты: запуск bulk-run (mock вью), проверка SSE.
- E2E в dashboard: тест, что запуск создаёт прогресс и даёт архив.
- Логи: добавить structured logging для bulk-run events; метрики Prometheus (bulk_jobs_total, bulk_jobs_duration).

## 10. Rollout
- 1) Развернуть миграции и обновить Docker-образы.
- 2) Прогнать smoke bulk-run на staging (2 площадки, лимит 10).
- 3) Проверить архив, прогресс, SSE.
- 4) Включить фичефлаг `BULK_RUN_ENABLED` в prod.
- 5) Мониторить метрики/логи, откатить (флаг OFF) при проблемах.

## 11. Риски и откаты
- Долгие архивы → ограничить размер, сохранять по частям.
- Ошибки job’ов → отображать failed state и позволять перезапуск площадки.
- Высокая нагрузка → лимит concurrency на уровень bulk-runner.
- Откат: отключить фичу, удалить таблицы (при необходимости), вернуть к одиночным запускам.

Все что дорабатыввается и исправляется должно быть протестировано и задокументировано. Если ты что-то меняешь - ты должен это протестировать и убедиться что всё работает. Ты должен уметь всё делать сам. 

Все доработки должны укомплектовываться в докер файлы так чтобы можео было развернуть всё с нуля на новом сервере. Все зависимости и прочие должны быть задокументированы и прописаны в докер файлах.