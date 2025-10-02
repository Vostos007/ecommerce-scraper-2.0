# PRD v1 — Unified Sitemap Scraping Service

## 1. Цель и ценность
Создать визуальный сервис на Next.js, который принимает sitemap (URL, XML, TXT, GZ, CSV) и orchestrates многослойный скрейпинг российских e-commerce сайтов (1C‑Bitrix, WordPress/WooCommerce, CS-Cart, InSales и аналоги). Платформа:
- запускает сбор по стратегии **cheap → expensive**: direct → datacenter proxy → antibot → FlareSolverr → residential burst;
- соблюдает бюджеты трафика, резидентских прокси и таймауты, сохраняя покрытие URL ≥ 95%;
- формирует три отчёта (CSV + Excel): `full`, `seo`, `diff` — совместимые со существующими экспортами (`data/sites/<domain>/exports/latest.{json,xlsx}`);
- обеспечивает наблюдаемость, health и переиспользование результатов (история job’ов, авто diff).

## 2. Персоны и сценарии
| Персона | Цель | Основные действия |
|---------|------|-------------------|
| Аналитик | Получить полные данные по каталогу | Загружает sitemap (файл/URL) → задаёт параметры (лимиты, allow_residential, Firecrawl) → запускает job → скачивает `full.csv/xlsx`, `diff.csv/xlsx` |
| Маркетолог / SEO | Отслеживает метаданные и изменения | Запускает job, фокусируется на `seo.csv/xlsx` и `diff` (статусы `ADDED/REMOVED/MODIFIED`) |
| Оператор | Контролирует бюджеты и стабильность | Мониторит прогресс в реальном времени (SSE логи, health, circuit breaker), управляет прокси пулом, анализирует ошибки |

## 3. Scope
### 3.1 Включено
- Загрузка sitemap: drag-n-drop, URL, поддержка XML/TXT/CSV/ZIP/GZ.
- Предпросмотр и фильтрация URL: маски исключений, лимит по количеству, дедуп, сортировка по приоритету.
- Стратегия **cheap→expensive** с конфигурируемыми бюджетами (трафик, residential квоты, таймауты).
- Экспорт трёх отчётов (CSV + XLSX): `full`, `seo`, `diff` (схемы см. §5.3).
- Dashboard: карточка job, статусы, прогресс, SSE логи, скачивание артефактов, история запусков.
- Поддержка CMS/движков (детекторы + шаблоны парсеров): Bitrix, WordPress (WooCommerce), CS-Cart, InSales + generic fallback.
- Антибот-цепочка: antibot flow, FlareSolverr интеграция, residential burst (по строгим квотам, разрешается флагом).
- Авто-дифф с последним успешным запуском; хранение снапшотов.
- Ручное управление proxy пулом: загрузка списков, типизация, health-check.
- Опция Firecrawl (вкл./выкл.) с вводом API ключа для fallback на сложных страницах.

### 3.2 Исключено (MVP)
- Авторизация/SSO, учётные записи (публичный UI “single tenant” с базовой auth/secret).
- Решение CAPTCHA сторонними сервисами (2captcha и т.п.).
- Сбор скриншотов/видео, download binary assets (css/js/img/pdf).
- Автоматический поиск sitemap — пользователь загружает карту сам (auto-detect опционален позже).
- Интеграция с BI/CRM (только скачивание файлов, REST hook добавим позднее).

## 4. Frontend UX (Next.js 15) 
### 4.1 Основные экраны
1. **Upload & Configure**
   - Поле `Введите URL домена`.
   - Поле `Upload sitemap` (accept: .xml, .txt, .csv, .gz/.zip). 
   - Checkbox `Использовать только загруженную карту` (иначе — авто-дополнение/парсинг sitemap.xml по URL).
   - Таблица предпросмотра (100 строк, lazy load) + фильтры (include/exclude patterns, max URLs).
   - Параметры job:
     - `Max URLs`, `Max concurrency`, `Per-URL timeout`.
     - `Traffic budget (MB)`, `Residential limit (MB)`.
     - `Allow residential burst` (toggle, default off).
     - `Enable Firecrawl fallback` (toggle + поле `Firecrawl API key`).
     - `Proxy strategy`: select (Auto / Custom list) + upload `.txt` (формат: `<scheme>://user:pass@host:port`).
     - `Proxy types` флажки для индикации: HTTP/HTTPS, SOCKS5, Datacenter, ISP, Residential, Mobile.
   - Кнопка `Запустить сбор`.

2. **Job Detail / Progress**
   - Лента SSE-логов (namespace: strategy step, proxy, статус).
   - Прогресс бар (URL total / success / failed, %).
   - Панели `Traffic budget`, `Residential usage`, `Error rate`, `Circuit breaker`.
   - Таблица ошибок (URL, статус, последний шаг стратегии, retry count).
   - Кнопки `Pause`, `Resume`, `Cancel` (при поддержке backend queue).
   - Блок `Exports`: ссылки на `full.csv`, `full.xlsx`, `seo.csv`, `seo.xlsx`, `diff.csv`, `diff.xlsx` + JSON снапшот.

3. **History / Runs**
   - Список job’ов (дата, автор, домен, кол-во URL, успех/ошибка, резидентский расход).
   - Возможность `Download artifacts`, `View diff`, `Clone settings`.

### 4.2 UX детали
- Валидация файлов (размер < 10 MB, формат), отображение количества URL после парсинга.
- Жалобы на robots.txt и Terms — уведомления/логирование с возможностью игнорирования (если пользователь явно разрешил).
- Tooltip’ы с объяснением стратегии и бюджета.
- Темing: светлая/тёмная, локализация ru/en (опционально).

## 5. Backend / Orchestration
### 5.1 Архитектура
- **API слой (Next.js API routes / FastAPI backend)**: принимает payload, валидирует (Pydantic), сохраняет metadata (PostgreSQL/SQLite), ставит job в очередь (Celery/RQ).
- **Job Queue**: Redis/RabbitMQ; worker тянет задания, инициализирует ScraperEngine.
- **ScraperEngine** (Python):
  - Загружает `config/domains/<domain>.yml` + `config/proxy/*.yml` (типизация прокси).
  - Построение стратегии: direct → datacenter → antibot → FlareSolverr → Firecrawl (если разрешено) → residential.
  - Управляет concurrency (per-domain limiter), budgets, circuit breaker.
  - Контролирует health прокси (ProxyHealthChecker) и PremiumProxyManager (авто-покупка **только** для non-residential).
  - Хранит результаты в `data/sites/<domain>/jobs/<job_id>/` (raw JSON, SEO JSON, HTML snapshots ≤ 1.5 MB).
  - Trigger’ит сравнение с предыдущим job (diff engine).
- **Diff Engine**: сравнивает JSON снапшоты, помечает изменения (field-level, status `ADDED/REMOVED/MODIFIED`), выводит `diff.csv/xlsx`.
- **Exporter**: использует существующий `utils/export_writers.py` + расширения для SEO, diff; генерирует CSV и XLSX (pandas/openpyxl).
- **Observability**: Prometheus metrics (`url_success_total`, `strategy_fallback_count`, `traffic_usage_bytes`), структурированные логи (JSON, masked secrets). SSE/WS слой выдаёт прогресс.

### 5.2 Интеграции и fallback
- **HTTPX client**: HTTP/2, keepalive, retries, respect robots (конфигурируемо).
- **curl_cffi** (impersonate) для TLS сложных сайтов.
- **AntibotManager**: cookie/session джар, Guard detection (Bitrix, Cloudflare, DDoS-GUARD).
- **FlareSolverr**: по сигналу антибота (max попыток), runtime health-check.
- **Firecrawl**: при включённом флаге и наличии API key — fallback `crawl` для проблемных URL (respect `zero_data_retention` flag, если доступен).
- **Proxy policy**: конфигурация типов (HTTP/HTTPS, SOCKS5, ISP, Residential, Mobile). Система health → rotation → burn list.

### 5.3 Форматы экспортов
- `full.csv/xlsx`
  - Столбцы: `url`, `status_code`, `cms_detected`, `product_data` (flatten JSON), `price`, `availability`, `timestamp`, `raw_html_path`.
- `seo.csv/xlsx`
  - `url`, `title`, `meta_description`, `meta_keywords`, `h1`, `canonical`, `og:title`, `og:description`, `og:image`, `twitter:*`, `img_alt_text_sample`, `last_modified`.
- `diff.csv/xlsx`
  - `url`, `change_type` (`ADDED`, `REMOVED`, `MODIFIED`), `field`, `old_value`, `new_value`, `timestamp_prev`, `timestamp_curr`.

Разделение по табам (CSV для быстрой загрузки в BI, XLSX — для ручной проверки). Используем существующие writer’ы, обновив схемы (pandas writer + openpyxl стили).

## 6. Нефункциональные требования
- **Надёжность**: Circuit breaker (по количеству HTTP 5xx/429), health-check прокси, перезапуск job idempotent (resume с чекпоинтов).
- **Производительность**: concurrency per domain ≤ 10 (конфиг), streaming парсер (lxml), early terminate (`</html>`, max size 1.5 MB). KPI: diff генерация < 30 сек при 10k URL (готовые снапшоты).
- **Экономия**: gzip/deflate/Br, content-type whitelist (`text/html`, `application/json`), дедуп URL, budgets (MB) и quotas (residential). ≥ 95% URL без residential, ≤ 5% job’ов используют residential burst.
- **Безопасность**: secrets в Secret Manager/.env (Firecrawl API, proxy creds), маскирование в логах (regex), TLS everywhere, audit log.
- **Комплаенс**: respect robots.txt (configurable flag), Terms acknowledgement (checkbox). Логируем нарушения.
- **Observability**: Prometheus/Grafana dashboard (traffic, success rate, fallback usage), alerting на превышение бюджетов.

## 7. HLD Overview
```
+----------------------+
| Next.js Frontend     |
| (Upload, Config)     |
+----------+-----------+
           |
           v
+----------+-----------+
|  REST API (FastAPI)  |
|  Validation + Auth   |
+----------+-----------+
           |
           v
+----------+-----------+      +-------------------+
|  Job Queue (Redis)   +----->| Worker (Scraper)  |
+----------+-----------+      |  HTTPX/Antibot    |
           |                  |  Firecrawl        |
           |<-----------------+  Exporter         |
           v
+----------------------+      +-------------------+
| Artifact Storage     |<-----+
| (data/sites/<domain>)|
+---------+------------+
          |
          v
+----------------------+        +-------------------+
| Diff / History DB    |<------>| Prometheus/Grafana|
+----------------------+        +-------------------+
```

## 8. Требования к прокси
- Типы:
  - **HTTP/HTTPS (datacenter)** — базовый пул, дешёвый.
  - **SOCKS5** — расширенный пул для специфических сайтов.
  - **ISP (static residential)** — промежуточный уровень (допускаем, но лимитируем).
  - **Residential (rotating)** — последний шаг, квоты MB/день и кол-во запросов.
  - **Mobile proxies** — опционально, использовать вручную при необходимости.
- UI: upload .txt (по строке), возможность добавить single proxy, менеджер активных прокси (health score, last used, type).
- Политики:
  - Авто-покупка разрешена только для datacenter/ISP (резидентские вручную).
  - Cookies/sessions isolating по типу прокси (residential → отдельный namespace).
  - Строгое логирование объёма трафика, cost.

## 9. Firecrawl интеграция
- Toggle `Enable Firecrawl fallback` (default off).
- Поле `Firecrawl API key` (masked, stored encrypted, zero-logging).
- Настройки: max бюджет запросов, `zero_data_retention` флаг (если у аккаунта).
- В pipeline: используется после FlareSolverr, если `allow_firecrawl` и лимиты не превышены. Результат merging в общий экспорт.

## 10. Diff и история
- Хранить `jobs/<job_id>/full.json` и `seo.json` (компактные JSON).
- Diff-engine сравнивает последнюю завершённую job (по домену) с текущей:
  - Если изменений нет — `diff.csv` содержит заголовок + `No changes detected`.
  - Для `MODIFIED` сохраняем список полей (пример: `price`, `availability`, `title`).
- UI: карточка job → вкладка `Diff preview` (первые 50 записей).

## 11. Принципы/политики эксплуатации
- Residential — только как **последняя линия** (строгие квоты, отдельные consent).
- Cookies не пересекаются между DC и residential pools.
- Не качаем статику/ассеты, ограничиваем размер HTML.
- Fail-fast: таймауты (connect/read) ≤ 30 сек, 3 retry с экспоненциальным backoff.
- Robots/Terms: по умолчанию уважаем; override требует явного подтверждения.

## 12. Acceptance Criteria
1. Пользователь загружает карту → выбирает параметры → запускает job.
2. На экране прогресса отображаются текущий шаг стратегии, бюджеты, ключевые ошибки.
3. По завершении доступны `full.*`, `seo.*`, `diff.*` (CSV+XLSX), diff корректно помечает изменения.
4. В истории — статус job, ссылки на артефакты, ресконструктор настроек.
5. ≥95% URL проходят без residential; residential usage логируется.
6. При повторном запуске diff формируется < 30 сек (10k URL, снапшоты присутствуют).
7. Прокси health-check и circuit breaker предотвращают массовые таймауты; SLA на ConnectTimeout ↓ ≥60%.

## 13. KPI
- Успех (200/OK) без residential ≥95% URL.
- Residential burst всего ≤5% job’ов.
- Среднее время job (10k URL) ≤ 40 минут при базовой стратегии.
- Ошибки ConnectTimeout ↓ ≥60% (после health rotation).
- Diff генерация < 30 сек для 10k URL (при наличии baseline).

## 14. Открытые вопросы / ToDo
- Выбор бекенда: оставаться на Next.js API routes или вынести FastAPI отдельно.
- Где хранить артефакты: локальные файлы или S3 (для прод).
- Выбор хранилища истории (SQLite → PostgreSQL?).
- Лицензирование/Terms сайта — нужен шаблон офферт/политики использования.
- UI локализация (ru/en) — включить ли в MVP?
- Automation для авто-поиска sitemap (на перспективу).

## 1.9. CSV спецификации
**Общие правила**: CSV в кодировке UTF-8, разделитель `,`, экранирование `"`. Даты в формате ISO 8601 (UTC). Пустые значения — пустая строка.

### full.csv
| Колонка      | Тип        | Nullable | Пример                     | Правила                                   |
|-------------|------------|---------:|----------------------------|-------------------------------------------|
| url         | string     |        ❌ | https://shop.ru/p/123      | канонический URL                          |
| final_url   | string     |        ✅ | https://shop.ru/p/123?utm= | после редиректов, если есть               |
| http_status | integer    |        ❌ | 200                        | HTTP код                                  |
| fetched_at  | datetime   |        ❌ | 2025-10-01T12:30:00Z       | UTC                                        |
| title       | string     |        ✅ | Кроссовки X                | trim, max 512 символов                     |
| h1          | string     |        ✅ | Кроссовки X                |                                           |
| price       | decimal    |        ✅ | 5990.00                    | точка как разделитель, округление 2 знака |
| currency    | string(3)  |        ✅ | RUB                        | ISO-4217                                   |
| availability| string     |        ✅ | in_stock                   | нормализованное значение                   |
| sku         | string     |        ✅ | SKU-123                    |                                           |
| brand       | string     |        ✅ | Nike                       |                                           |
| category    | string     |        ✅ | Обувь>Кроссовки            | `>` как разделитель                       |
| breadcrumbs | string     |        ✅ | Главная>Обувь>Кроссовки    | `>` как разделитель                       |
| images      | string     |        ✅ | url1|url2                  | `|` как разделитель                       |
| attrs_json  | json       |        ✅ | {"color":"черный"}      | сериализованный JSON                      |
| text_hash   | string(64) |        ✅ | 7f9a...                    | SHA-256 HTML-текста                       |

### seo.csv
| Колонка             | Тип      | Nullable | Пример              | Правила                |
|---------------------|----------|---------:|---------------------|------------------------|
| url                 | string   |        ❌| https://shop.ru/p/1 |                        |
| fetched_at          | datetime |        ❌| 2025-10-01T12:30:00Z| UTC                     |
| title               | string   |        ✅| Кроссовки X         | max 512                 |
| meta_description    | string   |        ✅| Лучшие кроссовки    | max 1024                |
| h1                  | string   |        ✅| Кроссовки X         |                        |
| og_title            | string   |        ✅| Кроссовки X         |                        |
| og_description      | string   |        ✅|                     |                        |
| og_image            | string   |        ✅| https://.../1.jpg   |                        |
| twitter_title       | string   |        ✅| Кроссовки X         |                        |
| twitter_description | string   |        ✅|                     |                        |
| canonical           | string   |        ✅| https://shop.ru/p/1 |                        |
| robots              | string   |        ✅| index,follow        |                        |
| hreflang            | string   |        ✅| ru-RU|en-US          | `|` как разделитель    |
| images_alt_joined   | string   |        ✅| вид1|вид2            | `|`, trim, max 512      |

### diff.csv
| Колонка           | Тип      | Nullable | Пример                           | Правила                       |
|-------------------|----------|---------:|----------------------------------|-------------------------------|
| url               | string   |        ❌| https://shop.ru/p/1              | ключ                         |
| prev_crawl_at     | datetime |        ✅| 2025-09-30T11:00:00Z             | UTC                          |
| curr_crawl_at     | datetime |        ❌| 2025-10-01T12:30:00Z             | UTC                          |
| change_type       | enum     |        ❌| ADDED/REMOVED/MODIFIED/UNCHANGED |                               |
| fields_changed    | string   |        ✅| price;availability               | `;` как разделитель          |
| price_prev        | decimal  |        ✅| 5490.00                          | округление 2 знака           |
| price_curr        | decimal  |        ✅| 5990.00                          |                               |
| availability_prev | string   |        ✅| in_stock                         | нормализация                 |
| availability_curr | string   |        ✅| out_of_stock                     |                               |
| title_prev        | string   |        ✅| Кроссовки X                      | усечение до 200 символов    |
| title_curr        | string   |        ✅| Кроссовки X                      | усечение до 200 символов    |

## 1.10. Правила сравнения (diff)
- Сопоставление по `url` (для вариаций — по `variation_url`).
- Перед сравнением строки нормализуются: trim, collapse пробелов, lower-case (оригинал сохраняем).
- Цены приводятся к decimal, запятые заменяются точкой, округление до 2 знаков.
- Множества (`images`, `attrs`) сравниваются без учёта порядка.
- `change_type` определяется: `ADDED` — нет записи в baseline; `REMOVED` — была, но отсутствует в текущем списке; `MODIFIED` — хотя бы одна контролируемая метрика изменилась; иначе `UNCHANGED`.

## 1.11. API
| Метод | Путь                  | Назначение        | Параметры / body                                                                                  | Ответ                                    |
|-------|-----------------------|-------------------|----------------------------------------------------------------------------------------------------|------------------------------------------|
| POST  | /api/jobs             | создать job       | `sitemap_url` или `sitemap_file`, `options{domain, allow_residential, limits{mb, concurrency}}`    | `201 { job_id }`                         |
| GET   | /api/jobs/:id         | статус job        | —                                                                                                  | `200 { status, progress, counters, ... }`|
| POST  | /api/jobs/:id/cancel  | остановить job    | —                                                                                                  | `202 { ok: true }`                       |
| GET   | /api/jobs/:id/exports | список артефактов | query `type=full|seo|diff`                                                                          | `200 { urls: [...] }`                    |
| GET   | /api/jobs/:id/stream  | SSE-лог           | —                                                                                                  | `text/event-stream`                      |
**Ошибки**: `400` (валидация, лимиты), `401/403` (auth), `409` (job уже выполняется), `413` (слишком большой файл), `429` (rate limit), `500`.

## 1.12. Лимиты загрузки
- Поддерживаемые форматы: XML, TXT, CSV, GZ (однопроходная компрессия).
- Максимальный размер файла — 50 MB, не более 200 000 URL.
- Все URL должны принадлежать одному домену (или подпрофилю).
- Защита от zip-bomb: отклоняем многослойные gzip/zip.

## 1.13. Доступ и юридические флаги
- Авторизация: заголовок `X-Admin-Token` (или JWT).
- Флаг `respect_robots` по умолчанию `true`; override возможен только в профиле домена + лог аудита.
- Пользователь при запуске подтверждает право на скрейпинг (checkbox).
