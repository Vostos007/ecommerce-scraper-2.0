# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a comprehensive e-commerce scraping platform with dual architecture: a Python FastAPI backend for orchestration and a Next.js dashboard for UI management. The system scrapes multiple e-commerce sites (Atmosphere Store, Sitting Knitting, Knitshop, Ili-Ili, Triskeli, Manefa) and provides centralized data export, proxy management, and real-time monitoring.

## Architecture

### Backend Services (`services/`)
- **API** (`services/api/`): FastAPI application with PostgreSQL + Redis
  - Main entry: `services/api/main.py`
  - Routes: jobs, exports, health, SSE
  - Database: asyncpg with connection pooling
  - Queue: RQ (Redis Queue) for background job processing

- **Worker** (`services/worker/`): RQ workers that execute scraping tasks
  - Entry point: `services/worker/worker.py`
  - Task orchestration: `services/worker/job_executor.py`

### Core Components
- **Scrapers** (`scripts/`): Site-specific fast exporters using async HTTP
  - Base class: `scripts/fast_export_base.py`
  - Site implementations: `*_fast_export.py` files
- **Parsers** (`parsers/`): Site-specific parsing logic
- **Utils** (`utils/`): Export writers (CSV/Excel), helpers
- **Database** (`database/`): Migration system with SQL files

### Frontend Dashboard
- Next.js application on port 3002
- Real-time updates via Server-Sent Events
- API integration at `http://localhost:8000`

## Development Commands

### Docker Stack (Recommended)
```bash
make up          # Start all services (PostgreSQL, Redis, API, Workers)
make down        # Stop all services
make logs        # View logs from all services
make migrate     # Run database migrations
make clean       # Clean Docker volumes and temp files
```

### Local Development
```bash
make install     # Install Python dependencies + Playwright
make api         # Run FastAPI locally (port 8000)
make worker      # Run worker locally
make test        # Run pytest
```

### Site Export Scripts
```bash
python -m scripts.atmosphere_fast_export
python -m scripts.manefa_fast_export
python -m scripts.knitshop_fast_export
python -m scripts.ili_ili_fast_export
python -m scripts.triskeli_fast_export
python -m scripts.cityknitting_fast_export
python -m scripts.proxy_stats_export
```

## Key Configuration Files

- **`.env`**: Database URLs, Redis settings, API keys
- **`config/sites.json`**: Site configurations and export settings
- **`config/users.json`**: User authentication settings
- **`docker-compose.yml`**: Full stack definition
- **`requirements.txt`**: Python dependencies

## Database Schema

Core tables managed through migrations:
- `jobs`: Scraping tasks with status tracking
- `pages`: Page content and metadata
- `snapshots`: Diff snapshots for change detection
- `exports`: Export artifacts (CSV/Excel)
- `metrics`: Time-series metrics

## API Endpoints

### Job Management
- `POST /api/jobs` - Create scraping job
- `GET /api/jobs` - List jobs
- `GET /api/jobs/{job_id}` - Job status
- `POST /api/jobs/{job_id}/cancel` - Cancel job

### Exports
- `GET /api/jobs/{job_id}/exports` - Job exports
- `GET /api/download/master/status` - Master workbook status

### Monitoring
- `GET /api/health` - Health check
- `GET /api/sites` - Site list and status
- `GET /api/summary` - Dashboard metrics
- `GET /api/proxy/stats` - Proxy infrastructure metrics

## Development Workflow

1. **Setup**: Copy `.env.example` to `.env` and configure
2. **Start Stack**: `make up` starts all dependencies
3. **Migrate**: `make migrate` applies database schema
4. **Develop**: Use `make api` and `make worker` for local development
5. **Test**: `make test` runs pytest suite

## Proxy Infrastructure

The system includes sophisticated proxy management:
- Rotating proxy pool with health monitoring
- Automatic procurement and burn detection
- Support for HTTP/SOCKS5 protocols
- Budget tracking and auto-scaling

## Export System

### Export Types
- **full.csv**: Complete product catalog
- **seo.csv**: SEO-optimized fields
- **diff.csv**: Changes since last export

### Export Formats
- CSV with UTF-8 encoding
- Excel with multiple sheets
- JSON for API responses

## Important Notes

- The project uses Python 3.11+ with async/await throughout
- Database connections use asyncpg with connection pooling
- All scraping scripts are designed to be run independently
- The dashboard provides real-time updates via SSE
- Proxy management includes automatic failover and scaling