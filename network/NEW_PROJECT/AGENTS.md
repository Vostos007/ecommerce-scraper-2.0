# Repository Guidelines

## Project Structure & Module Organization
- Backend FastAPI code lives in `services/api`; queue workers sit in `services/worker` and reuse shared scraping utilities under `network/` (keep new modules inside `network/NEW_PROJECT`).
- Frontend assets for the dashboard reside in `apps/dashboard` with supporting scripts in `apps/dashboard/scripts` and shared UI logic in `apps/dashboard/lib` and `stores`.
- Database helpers, migrations, and fixtures are in `database/`; configuration samples live under `config/` (`policies.example.yml`, domain overrides, env templates).
- Test suites mirror their runtime code: Python tests in `network/NEW_PROJECT/tests`, API tests in `tests/api`, frontend Vitest/Playwright specs in `apps/dashboard/{vitest.config.ts,e2e}`.

## Build, Test, and Development Commands
- `make install` installs Python dependencies and Playwright binaries; run before first local build.
- `make up` / `make down` start and stop the full stack (API, Redis, MinIO) via Docker Compose.
- `make api` and `make worker` run services directly using `.env`; pair them with `pytest` for quick iteration.
- Frontend workflow: `pnpm --dir apps/dashboard install`, then `pnpm --dir apps/dashboard dev` or `build`; use `pnpm --dir apps/dashboard lint` and `test` before committing UI changes.

## Coding Style & Naming Conventions
- Python targets 3.11+ with type hints and 4-space indentation; prefer `pydantic` models for request/response schemas and keep orchestration services under `network/NEW_PROJECT`.
- Follow FastAPI path naming (`/api/<resource>`), snake_case for modules, PascalCase for classes, and descriptive function names (`collect_proxy_metrics` over shorthand).
- In the dashboard, keep files colocated by feature, use camelCase for hooks/stores, PascalCase for components, and rely on Prettier/ESLint defaults (`pnpm lint`).

## Testing Guidelines
- Create Python tests beside the module (`tests/test_<module>.py`) and run `pytest` or targeted commands like `pytest network/NEW_PROJECT/tests/test_proxy_stats.py`.
- Frontend unit tests run with `pnpm --dir apps/dashboard test`; use `test:coverage` for CI parity and `test:e2e` for Playwright smoke checks.
- Maintain coverage over new flows (bulk runs, proxy metrics) and update fixtures under `data/` when schemas change.

## Commit & Pull Request Guidelines
- Follow Conventional Commits (`feat:`, `fix:`, `chore:`) as used in history (`feat: deliver real dashboard metrics and version badge`).
- PRs must link related tasks, summarize behaviour changes, note migrations or config updates, and include screenshots or curl snippets when UI/API output changes.
- Document architectural decisions in `logs/DECISIONS.md` and add run notes to `logs/DEV_LOG.md` (role selection, actions, results) before requesting review.

## Security & Configuration Notes
- Bootstrap local secrets from `.env.example`, adjust policies via `config/policies*.yml`, and confirm constraints in `tech_stack_policy.md` and `SECURITY.md`.
- Respect robots/proxy policies: do not enable residential flows or bypass robots without profile updates and audit entries.
- Archive outputs to the expected `data/sites/<domain>/exports` layout and verify artefacts against the formats defined in `reports_spec.md`.
