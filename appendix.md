# Приложение: структуры данных и планы

## 5. Структуры данных / схемы
### 5.1. Пример `config/proxy/site_profiles/ili-ili.com.yml`
```yaml
domain: "ili-ili.com"
allow_residential: true
budget:
  soft_mb_per_day: 50
  hard_mb_per_day: 100
  residential_mb_per_day: 150
fetch_policy:
  sequence: [direct, datacenter_proxy, antibot, flaresolverr, residential_burst]
guards:
  max_concurrent: 2
  disallow_patterns: ["*/static/*", "*/assets/*", "*.pdf", "*.jpg", "*.png", "*.css", "*.js"]
  content_type_whitelist: ["text/html", "application/json"]
antibot:
  detection: { check_cloudflare: true, check_captcha: true, analyze_headers: true }
  escalation: { cloudflare_detected: flaresolverr, captcha_detected: skip_or_manual }
timeouts: { connect: 30, read: 60, total: 60 }
retries: { total: 5, backoff: exponential }
```


### 5.1.1. Пример `config/proxy/site_profiles/spa-example.com.yml`
```yaml
domain: "spa-example.com"
allow_residential: false
budget:
  soft_mb_per_day: 30
  hard_mb_per_day: 60
fetch_policy:
  sequence: [headless_http, direct, datacenter_proxy, antibot]
guards:
  max_concurrent: 4
  disallow_patterns: ["*/api/internal/*"]
  content_type_whitelist: ["application/json"]
parsing:
  type: "json_api"
  root_selector: "$.data.items[*]"
  fields:
    - name: "id"
    - name: "attributes.title"
    - name: "attributes.price"
timeouts: { connect: 10, read: 30, total: 45 }
retries: { total: 2, backoff: linear }
```
### 5.2. Колонки CSV (расширяемый минимум)
### 5.3. Примеры CSV
**full.csv**
```csv
url,final_url,http_status,fetched_at,title,h1,price,currency,availability,sku,brand,category,breadcrumbs,images,attrs_json,text_hash
https://shop.ru/p/1,https://shop.ru/p/1,200,2025-10-01T12:30:00Z,"Кроссовки X","Кроссовки X",5990.00,RUB,in_stock,SKU-1,Nike,"Обувь>Кроссовки","Главная>Обувь>Кроссовки","https://.../1.jpg|https://.../2.jpg","{"color":"черный"}","7f9a..."
https://shop.ru/p/2,https://shop.ru/p/2,404,2025-10-01T12:31:00Z,"","",,,,"","","","","","a1b2..."
```
**seo.csv**
```csv
url,fetched_at,title,meta_description,h1,og_title,og_description,og_image,twitter_title,twitter_description,canonical,robots,hreflang,images_alt_joined
https://shop.ru/p/1,2025-10-01T12:30:00Z,"Кроссовки X","Лучшие кроссовки","Кроссовки X","Кроссовки X","","https://.../1.jpg","Кроссовки X","","https://shop.ru/p/1","index,follow","ru-RU|en-US","вид сбоку|вид сверху"
```
**diff.csv**
```csv
url,prev_crawl_at,curr_crawl_at,change_type,fields_changed,price_prev,price_curr,availability_prev,availability_curr,title_prev,title_curr
https://shop.ru/p/1,2025-09-30T11:00:00Z,2025-10-01T12:30:00Z,MODIFIED,"price;availability",5490.00,5990.00,in_stock,out_of_stock,"Кроссовки X","Кроссовки X"
```

- **full.csv** — `url, final_url, http_status, fetched_at, title, h1, price, currency, availability, sku, brand, category, breadcrumbs, images, attrs_json, text_hash`
- **seo.csv** — `url, fetched_at, title, meta_description, h1, og_title, og_description, og_image, twitter_title, twitter_description, canonical, robots, hreflang, images_alt_joined`
- **diff.csv** — `url, prev_crawl_at, curr_crawl_at, change_type, fields_changed, price_prev, price_curr, availability_prev, availability_curr, title_prev, title_curr`

## 6. Guardrails (что не должно работать)
- Нельзя уходить в бесконечные ретраи/редиректы (≤ 3 попытки).
- Нельзя использовать residential без явного разрешения профиля сайта и доступных квот.
- Нельзя хранить секреты в Git; логи обезличены, токены маскируются.
- Нельзя переносить cookies между типами прокси (residential ↔ datacenter).
- Нельзя нарушать robots.txt без явной per-site настройки.
- Нельзя превышать максимальный размер HTML (строгое ограничение чтения).

## 7. Планы релиза (roadmap)
### 7.1. CI/CD и тесты
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Node.js
        uses: actions/setup-node@v3
        with:
          node-version: "20"
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - run: npm ci && npx playwright install chromium
      - run: poetry install
      - run: make test
      - run: npm run scrape -- --sitemap ./examples/sample-sitemap.xml --max-urls 5
```

- **MVP (3 недели)**:
  - Неделя 1 — базовый скрейпинг (httpx + YAML конфиги), загрузка sitemap, TUI мониторинг.
  - Неделя 2 — очередь (Redis + RQ), стратегия cheap→expensive, CSV экспорты.
  - Неделя 3 — Next.js UI (прогресс/логи), базовые детекторы Bitrix/WP/CS-Cart/InSales.
- **v1.1 (4 недели)**:
  - Недели 1–2 — Prometheus/Grafana панели, расширенные health-метрики прокси.
  - Неделя 3 — auto-diff scheduler.
  - Неделя 4 — CI/CD интеграция (GitHub Actions, smoke test).
- **v1.2 (5 недель)**:
  - Недели 1–2 — плагины парсеров (GraphQL/JSON API).
  - Неделя 3 — auto-discover sitemap из robots.txt.
  - Недели 4–5 — per-domain UI templates, квоты по времени суток.
### 7.2. Per-environment overrides
```yaml
default:
  circuit_breaker:
    failures: 5
    half_open_after_sec: 300
production:
  circuit_breaker:
    failures: 3
    half_open_after_sec: 180
```

### 6. OpenAPI черновик
```yaml
openapi: 3.0.3
info: { title: Webscraper API, version: "1.0.0" }
paths:
  /api/jobs:
    post:
      summary: Create job
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                sitemap_url: { type: string, format: uri }
                options:
                  type: object
                  properties:
                    domain: { type: string }
                    allow_residential: { type: boolean, default: false }
                    limits:
                      type: object
                      properties:
                        mb: { type: integer, default: 100 }
                        concurrency: { type: integer, default: 2 }
      responses:
        '201': { description: Created }
  /api/jobs/{id}:
    get:
      summary: Get job status
      parameters:
        - { name: id, in: path, required: true, schema: { type: string } }
      responses:
        '200': { description: OK }
  /api/jobs/{id}/exports:
    get:
      summary: List export URLs
      parameters:
        - { name: id, in: path, required: true, schema: { type: string } }
        - { name: type, in: query, schema: { type: string, enum: [full, seo, diff] } }
      responses:
        '200': { description: OK }
  /api/jobs/{id}/cancel:
    post:
      summary: Cancel job
      parameters:
        - { name: id, in: path, required: true, schema: { type: string } }
      responses:
        '202': { description: Accepted }
```
