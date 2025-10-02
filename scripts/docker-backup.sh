#!/bin/bash
set -euo pipefail

BACKUP_ROOT="./backups"
mkdir -p "$BACKUP_ROOT"

timestamp() {
  date +%Y-%m-%d
}

full_backup() {
  local dir="$BACKUP_ROOT/$(timestamp)"
  mkdir -p "$dir"
  echo "Backing up database..."
  docker-compose exec dashboard sqlite3 /app/data/database/competitor.db ".backup /tmp/competitor_backup.db"
  docker cp "$(docker-compose ps -q dashboard)":/tmp/competitor_backup.db "$dir/competitor.db"

  echo "Backing up data..."
  tar -czf "$dir/data.tar.gz" data

  echo "Backing up config..."
  tar -czf "$dir/config.tar.gz" config

  echo "Backing up logs..."
  tar -czf "$dir/logs.tar.gz" logs

  echo "Copying environment configuration..."
  cp .env "$dir/.env"

  echo "Backup completed at $dir"
}

incremental_backup() {
  echo "Incremental backups not fully implemented; running full backup instead."
  full_backup
}

list_backups() {
  ls -1 "$BACKUP_ROOT"
}

restore_backup() {
  local date="$1"
  local dir="$BACKUP_ROOT/$date"
  if [[ ! -d "$dir" ]]; then
    echo "Backup $date not found"
    exit 1
  fi

  echo "Stopping services..."
  docker-compose down

  echo "Restoring data..."
  tar -xzf "$dir/data.tar.gz"
  tar -xzf "$dir/config.tar.gz"
  tar -xzf "$dir/logs.tar.gz"

  echo "Starting services..."
  docker-compose up -d
}

verify_backup() {
  local file="$1"
  if [[ -f "$file" ]]; then
    if gzip -t "$file" >/dev/null 2>&1; then
      echo "Verified $file"
    else
      echo "Failed to verify $file"
      exit 1
    fi
  fi
}

case "${1:-}" in
  full)
    full_backup
    ;;
  incremental)
    incremental_backup
    ;;
  list)
    list_backups
    ;;
  restore)
    restore_backup "${2:-}"
    ;;
  *)
    cat <<USAGE
Usage: $0 [full|incremental|list|restore <date>]
USAGE
    exit 1
    ;;
Esac
