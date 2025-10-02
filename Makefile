.PHONY: help install migrate up down logs test clean

help:
	@echo "📦 Webscraper NEW_PROJECT Makefile"
	@echo ""
	@echo "Available commands:"
	@echo "  make install    - Install Python dependencies locally"
	@echo "  make migrate    - Run database migrations"
	@echo "  make up         - Start all services with Docker Compose"
	@echo "  make down       - Stop all services"
	@echo "  make logs       - Show logs from all services"
	@echo "  make test       - Run tests"
	@echo "  make clean      - Clean up Docker volumes and temp files"
	@echo "  make api        - Run API locally (requires .env)"
	@echo "  make worker     - Run Worker locally (requires .env)"

install:
	@echo "📥 Installing dependencies..."
	pip install -r services/api/requirements.txt
	pip install -r services/worker/requirements.txt
	playwright install chromium
	@echo "✅ Dependencies installed"

migrate:
	@echo "🗄️  Running database migrations..."
	@if [ -z "$$DATABASE_URL" ]; then \
		echo "⚠️  DATABASE_URL not set, using default..."; \
		export DATABASE_URL="postgresql://scraper:scraper@localhost:5432/scraper"; \
	fi
	python database/migrate.py
	@echo "✅ Migrations applied"

up:
	@echo "🚀 Starting services..."
	docker-compose up -d
	@echo "✅ Services started"
	@echo ""
	@echo "📡 API:      http://localhost:8000"
	@echo "📚 API Docs: http://localhost:8000/api/docs"
	@echo "🪣 MinIO:    http://localhost:9001 (admin/minioadmin)"
	@echo ""
	@echo "Run 'make logs' to see logs"
	@echo "Run 'make migrate' to apply database migrations"

down:
	@echo "🛑 Stopping services..."
	docker-compose down
	@echo "✅ Services stopped"

logs:
	docker-compose logs -f

test:
	@echo "🧪 Running tests..."
	cd ../.. && pytest network/NEW_PROJECT/tests/ -v
	@echo "✅ Tests completed"

clean:
	@echo "🧹 Cleaning up..."
	docker-compose down -v
	rm -rf data/jobs/*
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✅ Cleanup complete"

api:
	@echo "🚀 Starting API locally..."
	@if [ ! -f .env ]; then \
		echo "⚠️  No .env file found, copying .env.example..."; \
		cp .env.example .env; \
	fi
	cd services/api && python -m uvicorn main:app --reload --port 8000

worker:
	@echo "🚀 Starting Worker locally..."
	@if [ ! -f .env ]; then \
		echo "⚠️  No .env file found, copying .env.example..."; \
		cp .env.example .env; \
	fi
	python services/worker/worker.py

baseline:
	@echo "📊 Generating baseline metrics..."
	@echo "TODO: Implement baseline metrics collection"