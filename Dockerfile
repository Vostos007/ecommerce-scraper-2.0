# syntax=docker/dockerfile:1.6

# Playwright образ уже содержит все браузеры и системные библиотеки
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency manifests first to leverage Docker layer cache
COPY services/api/requirements.txt ./services/api/requirements.txt
COPY services/worker/requirements.txt ./services/worker/requirements.txt

RUN pip install --no-cache-dir -r services/api/requirements.txt \
    && pip install --no-cache-dir -r services/worker/requirements.txt

# Copy application source
COPY core ./core
COPY utils ./utils
COPY parsers ./parsers
COPY scripts ./scripts
COPY database ./database
COPY services ./services
COPY config ./config
COPY data ./data

ENV PYTHONPATH="/app"

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
