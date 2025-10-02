# Технологический стек и политики

## 4.1. Стек
- **Frontend**: Next.js (App Router), React 19, Zustand/TanStack Query, Server-Sent Events (SSE).
- **Backend**: FastAPI (Python 3.11), uvicorn.
- **Queue**: Redis + RQ (по умолчанию) или Celery (при расширении).
- **Scraping**: httpx (HTTP/2), curl_cffi (impersonation/JA3), aiohttp (быстрые батчи), FlareSolverr, опционально Playwright для edge-кейсов.
- **Parsing**: lxml / selectolax, extruct/microdata для schema.org.
- **Data**: Postgres/SQLite, S3-совместимое хранилище или локальный FS.
- **Monitoring**: Prometheus + Grafana, структурированные JSON-логи.
- **Configs**: YAML (global, proxy, site profiles) + Pydantic-валидация.

## 4.2. Когда что используем
- `httpx` — дефолтный клиент (direct/DC прокси).
- Antibot flow — при `403/429/503/Cloudflare` ответах.
- FlareSolverr — если требуется JS-рендер или CF-челлендж.
- Residential — только если все предыдущие шаги не дали HTTP-ответ и квоты не исчерпаны.
- `curl_cffi` — для сложных TLS/JA3 кейсов, когда httpx не проходит.
- Playwright — точечно (последняя линия защиты), только если FlareSolverr не справился.

## 4.3. Чего не используем (MVP)
- Автоматические CAPTCHA-сервисы (captcha solving за деньги).
- Сбор бинарников/медиа (CSS/JS/IMG/PDF).
- Логи с полным HTML (храним хэши/метаданные).
- Автопокупку residential-прокси по умолчанию (только manual approval).

## 4.4. Политики квот и ограничителей

- **TrafficBudgeter**: soft-лимит при 80% месячного трафика (параллелизм=1, отключаем FlareSolverr), hard-лимит при 100% — стоп по типу трафика.
- **CircuitBreaker**: срабатывает после 5 подряд ConnectTimeout/HTTP 429/503/ECONNREFUSED; half-open через 5 минут.
- **ProxyHealth**: исключаем прокси после 3 подряд неудач, возвращаем через 10 минут; вес учитывает успех (%) и латентность.
- **Residential Burst**: максимум 5 запросов подряд; cooldown = предыдущий интервал × 1.5; сброс при успехе.
- **Max retries per URL**: `max_retries = 3` (1 direct + 2 fallback); при исчерпании URL помечается `failed`.
- **SLA по времени**: `fetch_latency_avg ≤ 30 сек`, `fetch_latency_p95 ≤ 45 сек`; превышение p95 → alert и снижение concurrency на 20%.
- **Успешность без residential**: `success_direct_ratio ≥ 95%`; иначе warning и пересмотр DC-пула.
- **FlareSolverr**: `flare_concurrency = 3`, `flare_retry = 1`; очередь с приоритетами (direct/DC → Flare solver).
- **Alert thresholds**: error rate > 5% за 5 мин; 429 responses > 3× baseline за 5 мин; FlareSolverr timeouts > 10% за 10 мин.
- **TrafficBudgeter**: soft-лимит при 80% месячного трафика (параллелизм=1, отключаем FlareSolverr), hard-лимит при 100% — стоп по типу трафика.
- **CircuitBreaker**: срабатывает после 5 подряд ConnectTimeout/HTTP 429/503/ECONNREFUSED; half-open через 5 минут.
- **ProxyHealth**: исключаем прокси после 3 подряд неудач, возвращаем через 10 минут; вес учитывает успех (%) и латентность.
- **Residential Burst**: максимум 5 запросов подряд; cooldown = предыдущий интервал × 1.5; сброс при успехе.

## 4.5. Версионирование и обновления
- Конфигурация хранится в `config/policies.yml` с полем `version` (например, `v1.0.0`).
- Любое изменение порогов/параметров сопровождается bump'ом минорной версии (`v1.0.1`, `v1.1.0`).
- При деплое логируется активная версия, чтобы отслеживать изменения в алертах и дашбордах.

## 4.6. Пример policies.yml
```yaml
version: v1.0.0
traffic_budget:
  soft_ratio: 0.8
  hard_ratio: 1.0
circuit_breaker:
  failure_window: 5  # кол-во подряд ошибок
  half_open_cooldown: "5m"
proxy_health:
  consecutive_failures: 3
  cooldown: "10m"
residential_burst:
  max_consecutive: 5
  cooldown_multiplier: 1.5
max_retries: 3
sla:
  fetch_latency_avg: "<=30s"
  fetch_latency_p95: "<=45s"
flare_solverr:
  concurrency: 3
  retry: 1
alert_thresholds:
  error_rate: { window: "5m", max: 0.05 }
  http_429_spike: { window: "5m", multiplier: 3 }
  flare_timeout: { window: "10m", max: 0.10 }
```
- Можно переопределять значения per-site через `config/domains/<domain>.yml` (секция `policies_override`).
- Для dev/staging/prod окружений используйте отдельные файлы (`policies.dev.yml`, `policies.prod.yml`) с fallback на базовый.

## 4.7. Мониторинг версий
- Экспортировать метрику `policies_version{value="v1.0.0"}` в Prometheus.
- При загрузке конфигурации писать в лог: `Loaded policies version v1.0.0`.
- Алерты включают `policies_version` в payload, чтобы понимать контекст изменений.

## 4.8. Процедура отката
1. Хранить предыдущие версии `policies.yml` в Git (tags `policies/v1.0.0`, `policies/v1.0.1`).
2. При необходимости отката выполнить `git checkout policies/v1.0.0 -- config/policies.yml` и перезагрузить сервис (graceful reload).
3. В логах и метриках зафиксировать факт отката (`policy_rollback_from=v1.0.1,to=v1.0.0`).

## 4.5. Version policy
| Компонент  | Мин | Таргет |
|------------|-----|--------|
| Node.js    | 20  | 22     |
| Python     | 3.11| 3.12   |
| Next.js    | 15  | 15.5   |
| React      | 19  | 19.x   |
| httpx      | 0.27| 0.27.x |
| aiohttp    | 3.9 | 3.10.x |
| curl_cffi  | 0.6 | 0.7.x  |
| Playwright | 1.47| 1.48.x |
| Redis      | 7   | 7.x    |
| Postgres   | 14  | 15     |

## 4.6. Security & compliance
- Зависимости обновляются Renovate/Dependabot; сканы: `pip-audit`, `npm audit`, `trivy` (Docker).
- Secret scanning: `gitleaks` в CI. SAST: `bandit`, `semgrep`.
- Маскирование секретов в логах, ограничение записи HTML-боди до 16 KB.
- Robots/TOS: `respect=true` по умолчанию; override — через профили доменов с аудитом.
- Логи и артефакты хранятся не дольше 30 дней в prod (14 дней в dev).

## 4.7. Политики окружений
- Отдельные файлы `config/policies.dev.yml`, `config/policies.prod.yml` (fallback на базовый).
- В dev/staging `allow_residential=false` и `flare_concurrency=1` по умолчанию.
- Для prod применяются жёсткие квоты, mandatory alerting.

