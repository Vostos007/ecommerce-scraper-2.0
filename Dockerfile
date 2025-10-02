# syntax=docker/dockerfile:1.6

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System dependencies for asyncpg/psycopg2
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifests first to leverage Docker layer cache
COPY services/api/requirements.txt ./services/api/requirements.txt
COPY services/worker/requirements.txt ./services/worker/requirements.txt

RUN pip install --no-cache-dir -r services/api/requirements.txt \
    && pip install --no-cache-dir -r services/worker/requirements.txt \
    && playwright install-deps chromium \
    && playwright install chromium

# Copy application source
COPY database ./database
COPY services ./services
COPY config ./config

ENV PYTHONPATH="/app"

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
