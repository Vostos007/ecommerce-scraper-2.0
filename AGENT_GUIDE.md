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
