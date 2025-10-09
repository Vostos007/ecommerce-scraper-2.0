# UI Dashboard

UI Dashboard — это Next.js 15.5.4 + React 19 приложение, обеспечивающее управление Python экспортерами, real-time мониторинг логов и контроль прокси инфраструктуры.

## Tech Stack
- **Frontend**: Next.js 15.5.4, React 19.1.1, TypeScript 5.9.2
- **UI**: кастомные shadcn-style компоненты (без официального пакета), Tailwind CSS v4.0.0-beta.7, lucide-react
- **State**: TanStack Query v5.87.1, Zustand v5.0.8
- **Testing**: Vitest 3.2.4, Testing Library, jsdom
- **Linting**: ESLint 9.17.0, Prettier 3.4.2

## Возможности
- 🚀 Запуск Python экспортов через веб-интерфейс
- 📊 Серверные логи в реальном времени через SSE
- 📁 Загрузка JSON карт сайтов прямо из UI
- 🧭 Типобезопасные API клиенты и строгая валидация Zod
- 🔐 Rate limiting и безопасный запуск subprocess
- 📱 Адаптивный тёмный UI с кастомными shadcn-style компонентами

## Быстрый старт (порт 3055)
```bash
make dashboard-dev
```

### Альтернатива без Make
```bash
cd apps/dashboard
pnpm run dashboard:dev
```

`dashboard-dev` автоматически проверяет наличие Python **3.11+**, создаёт `.env` из `.env.example`, устанавливает Python/Node зависимости (через `pip install -r requirements.txt` и `pnpm install`) и запускает Next.js dev сервер на `http://localhost:3050` с переменной `PYTHON_BIN`. Дополнительно поддерживаются переменные:

- `PORT` — переопределить порт (по умолчанию `3055`)
- `PYTHON_BIN` — явный путь к Python 3.11+
- `SKIP_DASHBOARD_INSTALL=1` — пропустить `pnpm install` (например, в CI)
- `DASHBOARD_BOOTSTRAP_MODE=check` — выполнить только проверки окружения (без запуска сервера)

## Docker запуск
- Минимальный UI: `docker compose -f docker-compose.min.yml up`
- Полный стек: `docker compose --profile cache --profile proxies --profile monitoring up`
- Скрипт автоматизации: `./scripts/docker-setup.sh dev --minimal` (или с профилями)

## Аутентификация
- По умолчанию dev-окружение запускается с выключенной проверкой (`DASHBOARD_AUTH_DISABLED=true`, `NEXT_PUBLIC_DASHBOARD_AUTH_DISABLED=true`), поэтому UI доступен сразу с ролью admin.
- Для проверки реальной авторизации выключите заглушку, установив обе переменные в `false`, и убедитесь, что `SECURE_COOKIES=false`/`FORCE_HTTPS=false` при работе по `http://localhost`.
- Базовая admin-запись уже присутствует в `config/users.json`, пароль: `Hollywool1023`. Вы можете задать другой через `cd apps/dashboard && pnpm auth:setup --username=<имя> --password=<пароль> --role=<роль>`.
- JWT секреты (`JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`) и путь к user-store (`USER_STORE_PATH`) настраиваются в `.env`.

### Production сборка
```bash
pnpm build
pnpm start
```

### Проверки качества
```bash
pnpm type-check
pnpm lint
pnpm test
pnpm test:coverage
```

## Проектная структура
```
app/                 # Next.js App Router
components/          # UI компоненты (shadcn-style стили)
components/providers # React Query и др. провайдеры
hooks/               # Custom hooks (React Query + Zustand)
lib/                 # API клиенты, валидации, процессы
stores/              # Zustand state
harness/             # E2E и интеграционные тесты
```

## Интеграция с Python
- UI вызывает Next.js API routes, которые запускают `python -u -m scripts.<site>_fast_export`
- Логи транслируются через `/api/streams/:jobId`
- `/api/upload` принимает JSON карты, валидированные Zod
- `/api/proxy/stats` запускает `scripts.proxy_stats_export` и возвращает метрики
- `/api/proxy/datacenter` управляет пулом datacenter‑прокси (`config/proxy/proxy_pools.yml`, `manual_proxies.txt`, `proxies_https.txt`)

## TDD Workflow
- Тесты пишутся Vitest + Testing Library
- Coverage threshold: 80% и выше
- Vitest UI (`pnpm test:ui`) для интерактивного дебага
- ESLint + Prettier обеспечивают единый стиль кода

## Предварительные требования
- Node.js ≥ 20 (LTS)
- pnpm ≥ 9 (проект использует lockfile формата 9)
- Python ≥ 3.11 (для запуска экспортных скриптов)

## Переменные окружения
- `PYTHON_BIN` — путь к Python интерпретатору (обязателен для запуска экспортов)
- `PORT` — порт Next.js dev сервера (по умолчанию 3050)
- `NEXT_PUBLIC_APP_URL` — публичный URL UI (для dev использовать `http://localhost:3055`)

## Дополнительно
- Проект рассчитан на Node.js >= 20
- Рекомендуемый менеджер пакетов — pnpm
- Tailwind v4 и кастомные shadcn-style компоненты настроены для быстрой разработки UI
