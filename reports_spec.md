# Reports Specification

Этот документ описывает, как формируются итоговые отчёты на основе примера `ВайбкодингАналитика.xlsx` и соответствующих CSV-файлов.

## 1. Excel Analytics Workbook
Актуальный шаблон: `network/NEW_PROJECT/ВайбкодингАналитика.xlsx`.

### 1.1 Структура листов
| Лист            | Назначение                                                                                   |
|-----------------|-----------------------------------------------------------------------------------------------|
| SUMMARY_KPI     | Общие KPI: количество позиций, уникальных URL, товарных остатков, суммарная стоимость стока. |
| SUMMARY_TOP     | Топ-20 позиций по суммарной стоимости запасов (`stock_value = price * stock`).               |
| CATALOG         | Нормализованный каталог: `url, name, price, stock, in_stock, scraped_at, stock_value`.        |
| SEO             | SEO-данные по каждому URL (см. §2).                                                           |
| PREV            | Шаблон для загрузки предыдущего периода (вставляется вручную перед сравнением).              |
| DIFF            | Автоматическое сравнение текущего и предыдущего периодов (формулы подтягивают данные из PREV).|

### 1.2 Ключевые вычисления
- `stock_value = price * stock` (валюта источника, обычно RUB).
- `price` и `stock` в `CATALOG` берутся с приоритетом вариаций (если есть `variation_price`/`variation_stock`).
- В листе `DIFF` рассчитываются `price_delta`, `stock_delta`, `change_type` (`ADDED`, `MODIFIED`, `UNCHANGED`).
- Условное форматирование подсвечивает рост/падение.

### 1.3 Порядок обновления
1. Скопировать текущие данные (`CATALOG`, `SEO`) из свежего скрейпа.
2. Вставить данные старого периода на лист `PREV` (те же колонки, что в `CATALOG`).
3. Лист `DIFF` автоматически пересчитает дельты и статус изменений.
4. Листы `SUMMARY_KPI` и `SUMMARY_TOP` обновятся автоматически (через сводные формулы).

## 2. CSV выходные файлы
Экспорт в CSV должен соответствовать структуре Excel.

### 2.1 full.csv
```
url,final_url,http_status,fetched_at,title,h1,price,stock,stock_value,currency,availability,sku,brand,category,breadcrumbs,images,attrs_json,text_hash,variation_id,variation_sku,variation_type,variation_value,variation_price,variation_stock,variation_in_stock,variation_attributes
```
- `price`, `stock`, `variation_price`, `variation_stock`, `stock_value` — числовые поля: в CSV выводятся с разделителем `;` и десятичной запятой, в Excel остаются числами (формат определяется локалью).
- `variation_*` — отдельная строка на каждую вариацию товара. Для товаров без вариаций столбцы остаются пустыми.
- `stock` — агрегированный остаток по карточке (из суммарного наличия вариаций или числового поля).
- `stock_value = price * stock` — общая стоимость остатка в валюте источника.

### 2.2 seo.csv
```
url,fetched_at,title,meta_description,h1,og_title,og_description,og_image,twitter_title,twitter_description,canonical,robots,hreflang,images_alt_joined
```
- Поля `canonical`, `robots`, `og_*`, `twitter_*`, `hreflang`, `images_alt_joined` должны собираться скрейпером (см. PRD).

### 2.3 diff.csv
```
url,prev_crawl_at,curr_crawl_at,change_type,fields_changed,price_prev,price_curr,availability_prev,availability_curr,title_prev,title_curr
```
- Алгоритм diff соответствует листу `DIFF` в Excel.
- `fields_changed` — список через `;` (price;availability;title).

## 3. Правила сравнения (Diff)
- Сопоставление по `url`.
- Строки нормализуются (trim, collapse пробелов, lower-case) — оригинальные значения сохраняются для отчёта.
- Цены приводятся к decimal с округлением до 2 знаков.
- Изображения/атрибуты сравниваются как множества (без учёта порядка).
- `change_type` определяется по контролируемым полям (`price`, `availability`, `title`, `text_hash`).

## 4. Интеграция в пайплайн
- Скрейпер сохраняет JSON-результаты в `data/jobs/<job_id>/...` → генератор отчётов преобразует их в Excel/CSV по текущей спецификации.
- Для CI/CD можно использовать smoke-тест: запуск `npm run scrape -- --sitemap ./examples/sample-sitemap.xml --max-urls 5`, затем генератор формирует Excel/CSV и проверяет структуру.

## 5. Версионирование шаблона отчётов
- Версия Excel/CSV схем синхронизируется с `config/policies.yml` (`reports_version`), фиксируется в логах и метриках.
- При изменениях структуры обновляется контрольный Excel (в каталоге `network/NEW_PROJECT/`).
- Старые версии доступны через Git-теги (`reports/v1.0.0` и т.д.).
