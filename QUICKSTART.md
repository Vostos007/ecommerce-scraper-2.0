# Quickstart Guide: NEW_PROJECT Scraping Stack

Complete guide to launching the full scraping infrastructure from zero to running job in under 5 minutes.

## Prerequisites

- Docker & Docker Compose installed
- Git repository cloned
- Terminal access

## Architecture Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Dashboard  │────▶│  FastAPI API │────▶│  PostgreSQL │
│  (Next.js)  │     │  (Port 8000) │     │  (Port 5432)│
└─────────────┘     └──────────────┘     └─────────────┘
                            │
                            ▼
                    ┌──────────────┐
                    │ Redis Queue  │
                    │ (Port 6379)  │
                    └──────────────┘
                            │
                            ▼
                    ┌──────────────┐     ┌──────────────┐
                    │ Worker Pool  │────▶│ FlareSolverr │
                    │  (x2 replicas)     │  (Port 8191) │
                    └──────────────┘     └──────────────┘
```

## Quick Start (3 Commands)

```bash
# 1. Start all services
docker-compose up -d

# 2. Check services are healthy
docker-compose ps

# 3. Run database migrations
docker-compose exec api python network/NEW_PROJECT/database/migrate.py
```

**Expected output:**
```
NAME                 STATUS              PORTS
scraper_postgres     Up (healthy)        5432
scraper_redis        Up (healthy)        6379
scraper_api          Up                  8000
scraper_worker_1     Up
scraper_worker_2     Up
flaresolverr         Up                  8191
scraper_dashboard    Up                  3050
```

## Service Endpoints

| Service | URL | Purpose |
|---------|-----|---------|
| API | http://localhost:8000 | REST API for job management |
| API Docs | http://localhost:8000/docs | Interactive Swagger UI |
| Dashboard | http://localhost:3050 | Web UI for monitoring |
| FlareSolverr | http://localhost:8191 | Anti-bot challenge solver |
| PostgreSQL | localhost:5432 | Database (user: scraper, pass: scraper) |
| Redis | localhost:6379 | Job queue |

## Testing the Pipeline

### 1. Health Check

```bash
curl http://localhost:8000/api/health
```

Expected response:
```json
{
  "status": "healthy",
  "database": "connected",
  "redis": "connected",
  "timestamp": "2025-10-01T19:00:00Z"
}
```

### 2. Create a Job

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "sitemap_urls": [
      "https://example.com/product-1",
      "https://example.com/product-2",
      "https://example.com/product-3"
    ],
    "options": {
      "domain": "example.com",
      "max_concurrency": 2,
      "allow_residential": false
    }
  }'
```

Expected response:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "domain": "example.com",
  "total_urls": 3,
  "created_at": "2025-10-01T19:00:00Z"
}
```

### 3. Monitor Job Progress

```bash
# Replace JOB_ID with actual ID from step 2
JOB_ID="550e8400-e29b-41d4-a716-446655440000"

curl http://localhost:8000/api/jobs/$JOB_ID
```

Response states:
- `queued` → Job waiting for worker
- `running` → Worker processing URLs
- `succeeded` → All URLs processed successfully
- `failed` → Job encountered errors

### 4. Download Exports

```bash
# List available exports
curl http://localhost:8000/api/jobs/$JOB_ID/exports

# Response:
# [
#   {
#     "type": "full",
#     "format": "csv",
#     "path": "data/jobs/550e8400-.../full.csv",
#     "size_bytes": 12345
#   },
#   {
#     "type": "seo",
#     "format": "csv",
#     "path": "data/jobs/550e8400-.../seo.csv",
#     "size_bytes": 5678
#   }
# ]
```

Export files location: `data/jobs/{job_id}/`

## Export Formats

### full.csv
Complete product data with all fields:
- URL, HTTP status, fetched timestamp
- Title, H1, price, currency
- SKU, brand, category
- Availability, images, attributes

### seo.csv
SEO-specific metadata:
- Meta tags (title, description)
- Open Graph tags
- Twitter Cards
- Canonical URLs
- Hreflang

### diff.csv
Changes between crawls:
- Price changes
- Stock availability changes
- Title/content updates

## Monitoring & Logs

### View Service Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f api
docker-compose logs -f worker
docker-compose logs -f postgres
```

### Check Worker Status

```bash
# View active workers
docker-compose ps worker

# Scale workers up/down
docker-compose up -d --scale worker=4
```

### Database Access

```bash
# Connect to PostgreSQL
docker-compose exec postgres psql -U scraper -d scraper

# Example queries:
# \dt                           -- List tables
# SELECT * FROM jobs LIMIT 10;  -- Recent jobs
# SELECT * FROM pages LIMIT 10; -- Scraped pages
```

### Redis Queue Inspection

```bash
# Connect to Redis
docker-compose exec redis redis-cli

# Check queue length
LLEN queue:jobs

# View pending jobs
LRANGE queue:jobs 0 10
```

## Troubleshooting

### Services Won't Start

```bash
# Check logs for errors
docker-compose logs

# Restart specific service
docker-compose restart api

# Full reset
docker-compose down
docker-compose up -d
```

### Database Connection Errors

```bash
# Verify Postgres is healthy
docker-compose ps postgres

# Re-run migrations
docker-compose exec api python network/NEW_PROJECT/database/migrate.py

# Check connection
docker-compose exec postgres pg_isready -U scraper
```

### Worker Not Processing Jobs

```bash
# Check worker logs
docker-compose logs -f worker

# Verify Redis connection
docker-compose exec redis redis-cli PING

# Restart workers
docker-compose restart worker
```

### FlareSolverr Issues

```bash
# Check FlareSolverr status
curl http://localhost:8191/

# View FlareSolverr logs
docker-compose logs -f flaresolverr

# Restart service
docker-compose restart flaresolverr
```

## Configuration

### Environment Variables

Create `.env` file in project root:

```bash
# Database
DATABASE_URL=postgresql://scraper:scraper@postgres:5432/scraper

# Redis
REDIS_URL=redis://redis:6379/0

# FlareSolverr
FLARESOLVERR_URL=http://flaresolverr:8191

# API
API_BASE_URL=http://localhost:8000

# Worker
WORKER_CONCURRENCY=2
WORKER_TIMEOUT=300
```

### Scaling Workers

```bash
# Scale to 4 workers
docker-compose up -d --scale worker=4

# Scale to 1 worker
docker-compose up -d --scale worker=1
```

### Custom Scraper Configuration

Edit `config/settings.json`:
```json
{
  "scraping": {
    "backend": "httpx",
    "timeout": 30,
    "max_retries": 3
  },
  "batch_processing": {
    "enabled": true,
    "batch_size": 10
  }
}
```

## Development Workflow

### Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run integration tests
pytest tests/integration/ -v

# Run specific test
pytest tests/integration/test_full_pipeline.py::test_full_job_pipeline -v
```

### Hot Reload (Development Mode)

Services already configured with hot reload:
- API: Code changes in `services/api/` reload automatically
- Worker: Restart worker after code changes

```bash
docker-compose restart worker
```

### Accessing Services Directly

```bash
# Run commands in API container
docker-compose exec api python -c "from services.api.main import app; print(app)"

# Run worker manually
docker-compose exec worker python services/worker/worker.py
```

## Production Deployment

### Pre-Production Checklist

- [ ] Change default passwords in `.env`
- [ ] Set `JWT_SECRET` to secure random string
- [ ] Configure proper DATABASE_URL with strong password
- [ ] Set up SSL/TLS certificates
- [ ] Configure firewall rules
- [ ] Set up log rotation
- [ ] Configure backup strategy for Postgres
- [ ] Set up monitoring (Prometheus/Grafana)

### Recommended Production Settings

```yaml
# docker-compose.prod.yml
services:
  postgres:
    environment:
      POSTGRES_PASSWORD: ${SECURE_DB_PASSWORD}
    volumes:
      - /mnt/data/postgres:/var/lib/postgresql/data
  
  api:
    environment:
      DATABASE_URL: postgresql://scraper:${SECURE_DB_PASSWORD}@postgres:5432/scraper
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '2'
          memory: 2G
  
  worker:
    deploy:
      replicas: 10
      resources:
        limits:
          cpus: '1'
          memory: 1G
```

## Support & Documentation

- **API Reference**: http://localhost:8000/docs
- **Implementation Plan**: `network/NEW_PROJECT/IMPLEMENTATION_PLAN.md`
- **Database Schema**: `network/NEW_PROJECT/database/migrations/001_create_jobs_schema.sql`
- **Architecture**: `network/NEW_PROJECT/IMPLEMENTATION_ROADMAP.md`

## Next Steps

1. ✅ Services running
2. ✅ Job created and completed
3. ✅ Exports generated

Now you can:
- Integrate with your existing scrapers
- Customize export formats
- Add custom parsers
- Scale workers based on load
- Set up monitoring dashboards