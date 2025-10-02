#!/usr/bin/env bash
set -euo pipefail

echo "[update] Pulling latest git changes..."
git pull

echo "[update] Rebuilding containers..."
docker compose build --pull

echo "[update] Starting services..."
docker compose up -d --force-recreate --remove-orphans

echo "[update] Done."
