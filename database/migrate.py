#!/usr/bin/env python3
"""Database migration runner."""
import os
import sys
from pathlib import Path
from typing import List
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_connection(database_url: str):
    """Create database connection."""
    return psycopg2.connect(database_url)


def create_migrations_table(conn):
    """Create migrations tracking table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
    conn.commit()


def get_applied_migrations(conn) -> List[str]:
    """Get list of applied migrations from the database."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [row[0] for row in cur.fetchall()]


def apply_migration(conn, migration_file: Path):
    """
    Apply a single migration file.
    
    Args:
        conn: Database connection
        migration_file: Path to SQL migration file
    """
    version = migration_file.stem
    print(f"Applying migration: {version}")
    
    with open(migration_file, 'r', encoding='utf-8') as f:
        sql = f.read()
    
    with conn.cursor() as cur:
        # Execute migration SQL
        cur.execute(sql)
        # Record migration as applied
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (version,)
        )
    conn.commit()
    print(f"✅ Applied: {version}")


def main():
    """Main migration runner function."""
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://scraper:scraper@localhost:5432/scraper"
    )
    
    print(f"Connecting to database...")
    try:
        conn = get_connection(database_url)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        sys.exit(1)
    
    # Create migrations tracking table
    create_migrations_table(conn)
    
    # Get list of applied migrations
    applied = get_applied_migrations(conn)
    print(f"Applied migrations: {len(applied)}")
    
    # Get all migration files
    if not MIGRATIONS_DIR.exists():
        print(f"❌ Migrations directory not found: {MIGRATIONS_DIR}")
        sys.exit(1)
    
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    
    if not migration_files:
        print("⚠️  No migration files found")
        conn.close()
        return
    
    # Apply pending migrations
    pending_count = 0
    for migration_file in migration_files:
        version = migration_file.stem
        if version not in applied:
            try:
                apply_migration(conn, migration_file)
                pending_count += 1
            except Exception as e:
                print(f"❌ Failed to apply migration {version}: {e}")
                conn.close()
                sys.exit(1)
        else:
            print(f"⏭️  Skipped (already applied): {version}")
    
    conn.close()
    
    if pending_count > 0:
        print(f"\n✅ Applied {pending_count} migration(s)")
    else:
        print("\n✅ All migrations up to date")


if __name__ == "__main__":
    main()