# syntax=docker/dockerfile:1.6

# Playwright образ уже содержит браузеры и системные зависимости
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency manifest first to leverage Docker layer cache
COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Copy application source
COPY core ./core
COPY utils ./utils
COPY parsers ./parsers
COPY scripts ./scripts
COPY database ./database
COPY services ./services
COPY config ./config
COPY reports ./reports
COPY apps/dashboard/public ./apps/dashboard/public
COPY data ./data

ENV PYTHONPATH="/app"

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
