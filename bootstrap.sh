#!/usr/bin/env bash
set -euo pipefail

REPO_URL="git@github.com:Vostos007/ecommerce-scraper-2.0.git"
TARGET_DIR="${1:-$HOME/ecommerce-scraper-runtime}"

if [ ! -d "$TARGET_DIR/.git" ]; then
  mkdir -p "$TARGET_DIR"
  if [ -z "${2:-}" ]; then
    git clone "$REPO_URL" "$TARGET_DIR"
  else
    git clone "$REPO_URL" "$TARGET_DIR" >/dev/null
  fi
else
  git -C "$TARGET_DIR" pull --ff-only
fi

cd "$TARGET_DIR"

docker compose build

docker compose up -d

if command -v open >/dev/null 2>&1; then
  open "http://localhost:3000" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:3000" >/dev/null 2>&1 || true
fi

echo "Dashboard: http://localhost:3000"
echo "API Swagger: http://localhost:8000/api/docs"
