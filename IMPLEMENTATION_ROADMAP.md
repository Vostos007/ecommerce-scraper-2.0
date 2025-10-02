# Implementation Roadmap — Executive Summary

> **Создано**: 2025-10-01  
> **Статус**: 🟡 50% готовности к MVP  
> **Полный план**: [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)

---

## 🎯 Критические блокеры

Анализ выявил **3 критических пробела** в архитектуре, которые блокируют MVP:

| # | Компонент | Статус | Приоритет | Время |
|---|-----------|--------|-----------|-------|
| 1 | **FastAPI Backend Service** | ❌ Отсутствует | 🔥 P0 | 3 дня |
| 2 | **Worker Pool + Job Queue** | ❌ Отсутствует | 🔥 P0 | 3 дня |
| 3 | **Database Schema (jobs/pages/snapshots)** | ❌ Отсутствует | 🔥 P0 | 2 дня |
| 4 | **FlareSolverr Integration** | ⚠️ Контейнер есть, кода нет | 🟡 P1 | 1 день |

**Текущая архитектура**: Next.js API routes → spawn Python процессы  
**Требуемая архитектура**: FastAPI backend → Redis Queue → RQ Workers → Database

---

## ✅ Что работает отлично

| Компонент | Прогресс | Примечания |
|-----------|----------|------------|
| ScraperEngine | 95% | Стратегия cheap→expensive, batch processing |
| ProxyManager | 90% | Health-check, residential burst, traffic quotas |
| CMS Detectors | 100% | Bitrix, WordPress, CS-Cart, InSales, OpenCart |
| Next.js Dashboard | 85% | SSE logs, TanStack Query, auth |
| Export Writers | 65% | ⚠️ Не все колонки из PRD §1.9 |

---

## 📅 План реализации (2 недели)

### **Неделя 1**

#### Phase 1: Database Schema & Migrations (дни 1-2)
- [ ] Создать таблицы: `jobs`, `pages`, `snapshots`, `exports`, `metrics`
- [ ] Написать [`database/migrate.py`](database/migrate.py) runner
- [ ] Обновить [`database/manager.py`](database/manager.py) методами CRUD для jobs
- [ ] **Acceptance**: миграции idempotent, smoke test создания job

#### Phase 2: FastAPI Backend (дни 3-5)
- [ ] Создать структуру [`services/api/`](services/api/)
- [ ] Endpoints: `POST /api/jobs`, `GET /api/jobs/:id`, `POST /api/jobs/:id/cancel`
- [ ] Интеграция с Redis Queue (RQ)
- [ ] SSE endpoint для live logs
- [ ] **Acceptance**: API запускается, job enqueue'ится в Redis

### **Неделя 2**

#### Phase 3: Worker Pool Service (дни 1-3)
- [ ] Создать [`services/worker/worker.py`](services/worker/worker.py) (RQ Worker)
- [ ] Task [`scrape_job_task`](services/worker/tasks.py) с orchestration
- [ ] Интеграция с `ScraperEngine`
- [ ] Сохранение результатов в `pages` таблицу
- [ ] **Acceptance**: worker обрабатывает job, обновляет статус

#### Phase 4: Integration & Testing (дни 4-5)
- [ ] Интегрировать FlareSolverr в fallback chain
- [ ] Привести Export Schema к соответствию PRD (колонки)
- [ ] E2E test: создать job → обработать → сгенерировать экспорты
- [ ] Обновить Dashboard для работы с новым API
- [ ] **Acceptance**: полный pipeline работает end-to-end

---

## 🚀 Quick Start (после реализации)

```bash
# 1. Применить миграции
python database/migrate.py

# 2. Запустить сервисы
docker compose up -d

# 3. Проверить health
curl http://localhost:8000/api/health

# 4. Создать job
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "sitemap_urls": ["https://shop.ru/product1"],
    "options": {"domain": "shop.ru", "max_concurrency": 2}
  }'

# 5. Открыть Dashboard
open http://localhost:3000
```

---

## 📊 Success Metrics

| Метрика | Текущее | Цель MVP |
|---------|---------|----------|
| Архитектурная готовность | 50% | 100% |
| Job orchestration | 0% | 100% (idempotent, resumable) |
| Export compliance | 65% | 100% (все колонки PRD §1.9) |
| FlareSolverr integration | 10% | 100% (в fallback chain) |
| E2E test coverage | 0% | ≥1 passing test |

---

## 🎯 Приоритизация задач

### 🔥 **Критические (блокируют MVP)**
1. Database Schema для jobs — без этого нет persistence
2. FastAPI Backend — без этого нет API
3. Worker Pool — без этого нет асинхронной обработки
4. FlareSolverr integration — нужно для прохождения Cloudflare

### ⚠️ **Важные (снижают качество)**
5. Export Schema compliance — отчёты не соответствуют спецификации
6. Diff Engine improvements — нет нормализации строк
7. Health checks & monitoring — нужно для production

### 📌 **Желательные (улучшения)**
8. Firecrawl UI toggle — пока manual config
9. Prometheus metrics — мониторинг в будущем
10. Multi-worker scaling — после MVP

---

## 📚 Документация проекта

| Документ | Назначение |
|----------|------------|
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Детальный технический план (960 строк) |
| [`prd.md`](prd.md) | Product Requirements Document |
| [`Architecture.md`](Architecture.md) | Системная архитектура |
| [`tech_stack_policy.md`](tech_stack_policy.md) | Технологический стек и политики |
| [`reports_spec.md`](reports_spec.md) | Спецификация отчётов (CSV/Excel) |
| [`AGENT_GUIDE.md`](AGENT_GUIDE.md) | Руководство для агентов |

---

## 🔄 Статус фаз

| Фаза | Компонент | Статус | Дата начала | Дата завершения |
|------|-----------|--------|-------------|-----------------|
| 0 | Анализ и планирование | ✅ Completed | 2025-10-01 | 2025-10-01 |
| 1 | Database Schema | 🟡 Pending | — | — |
| 2 | FastAPI Backend | 🟡 Pending | — | — |
| 3 | Worker Pool | 🟡 Pending | — | — |
| 4 | Integration | 🟡 Pending | — | — |

---

## 👥 Рекомендованные роли агентов

| Фаза | Роли | Причина |
|------|------|---------|
| Phase 1 | `database-admin`, `database-optimizer` | Миграции, схемы |
| Phase 2 | `backend-architect`, `backend-security-coder` | FastAPI, API design |
| Phase 3 | `backend-architect`, `performance-engineer` | Workers, concurrency |
| Phase 4 | `test-automator`, `code-reviewer`, `performance-engineer` | Integration tests |

---

## ⚡ Следующие шаги

1. **Review** этого roadmap с командой
2. **Approve** архитектурный план ([`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md))
3. **Start Phase 1**: создание database schema
4. **Track progress**: обновлять TODO-лист после каждой фазы

---

**Готов начать реализацию?** Запустите:

```bash
# Переключиться в режим code и начать Phase 1
# Или делегировать database-admin агенту
```

> **Контакт**: См. [`logs/DEV_LOG.md`](../../logs/DEV_LOG.md) для текущих решений  
> **Issues**: Все блокеры задокументированы в [`logs/DECISIONS.md`](../../logs/DECISIONS.md)