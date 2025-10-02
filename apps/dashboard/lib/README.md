# Lib Layer Overview

## Обзор lib модулей

- `paths.ts` — нормализует пути, определяет корень проекта по `pyproject.toml`, предоставляет `resolveRepoPath` и `ensureDirectoryExists`.
- `utils.ts` — общие хелперы (`cn`, `formatBytes`, `sleep`, `sanitizeString`, `generateId`).
- `sites.ts` — загрузка `config/sites.json`, кэширование метаданных, валидация доменов и путей `data/sites/**`.
- `rate-limit.ts` — in-memory Token Bucket limiter для API endpoints.
- `validations.ts` — Zod схемы для JSON карт, параметров запуска экспортов и ответов proxy stats.
- `processes.ts` — запуск Python CLI скриптов из `scripts/*_fast_export.py`, управление subprocess, буфер логов и подписчики для SSE.

## Public API контракты

### paths.ts
- `getProjectRoot(): string` — возвращает абсолютный путь до корня репозитория.
- `resolveRepoPath(...segments: string[]): string` — собирает путь относительно корня.
- `ensureDirectoryExists(dirPath: string): Promise<void>` — гарантирует существование каталога.

### utils.ts
- `cn(...classes)` — конкатенация классов.
- `formatBytes(bytes, decimals?)` — человекочитаемый размер.
- `sleep(ms)` — Promise-delay.
- `sanitizeString(input)` — очищает строки для путей.
- `generateId()` — UUID для jobId.

### sites.ts
- `getSiteSummaries(): SiteSummary[]` — кэшируемые метаданные сайтов.
- `getSiteByDomain(domain)` — нормализованный поиск конфига.
- `assertSiteAllowed(domain)` — выбрасывает ошибку для неразрешенных доменов.
- `sanitizeSite(input)` — фильтрация пользовательских значений.
- `getSiteDirectory(domain)` — путь к `data/sites/{domain}` с авто-созданием.
- `getExportPath(domain)` — путь к `exports/latest.xlsx`.

### rate-limit.ts
- `TokenBucketLimiter` — `take(identifier)`, `reset(identifier)`, `getStats()`.

### validations.ts
- `uploadJsonSchema` — валидация JSON карт.
- `exportConfigSchema` — проверка параметров запуска.
- `proxyStatsSchema` — проверка ответа `/api/proxy/stats`.
- `urlSchema`, `domainSchema`, `timestampSchema` — строительные блоки.

### processes.ts
- `spawnExport(site, options)` — запуск `python -u -m scripts.<site>_fast_export` с поддержкой `--concurrency`, `--resume/--no-resume`.
- `getProcess(jobId)` — чтение процесс-рекорда для SSE.
- `stopProcess(jobId, signal?)` — graceful shutdown.
- `subscribeToProcess(jobId, subscriber, options?)` — подписка на stdout/stderr и завершение, реплей истории логов.
- `getPythonBinary()` — выбор интерпретатора по `PYTHON_BIN` → `python3` → `python`.

## Интеграция с Python CLI

`spawnExport()` собирает аргументы `['-u', '-m', 'scripts.<script>', '--concurrency', '--resume']`, запускает процесс с `cwd = getProjectRoot()`. stdout/stderr построчно буферизуются, попадают в лог-буфер и отдаются SSE подписчикам. Завершение процесса фиксируется и триггерит очистку через reaper. IncrementalWriter resume-потоки работают благодаря пробросу флагов `--resume/--no-resume`.

## SSE Architecture

- Процессы отслеживаются в `activeProcesses: Map<jobId, ProcessRecord>`.
- Логи сохраняются в ring-buffer до `LOG_BUFFER_SIZE` записей.
- `subscribeToProcess` сразу реплеит историю и слушает новые записи.
- SSE события имеют типы `stdout`, `stderr`, `end`; payload содержит `message`, `ts`, `code`, `signal`.
- При `AbortSignal` или client disconnect подписка отписывается, процесс завершает работу (SIGTERM). Завершенные процессы очищаются спустя 1 час, что удерживает память под контролем.

Документ служит контрактом между UI Dashboard и backend частью, фиксируя surface area lib-слоя.
