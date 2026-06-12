"""
alembic/env.py
──────────────
Alembic migration environment.

Driver split (intentional):
  • psycopg2  — used here, by the Alembic CLI, for synchronous DDL operations.
  • asyncpg   — used by the bot at runtime (asyncpg.create_pool).

Both accept the same postgresql:// URL, so DATABASE_URL in .env works for both
without any conversion.

Alembic does NOT use an ORM / MetaData object here.  All migrations are written
as raw SQL (op.execute(...)) in the versions/ files.  This keeps the migration
layer independent of any Python model changes.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# ── Path setup ───────────────────────────────────────────────
# Ensure the project root is on sys.path so `from bot.config import settings`
# resolves whether alembic is run from the project root or a subdirectory.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from bot.config import settings  # noqa: E402

# ── Alembic config object ─────────────────────────────────────
config = context.config

# Apply logging config from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject DATABASE_URL from pydantic settings.
# settings.asyncpg_dsn returns a plain postgresql:// string — SQLAlchemy
# defaults to psycopg2 for that scheme, which is what Alembic needs.
config.set_main_option("sqlalchemy.url", settings.asyncpg_dsn)

# No ORM MetaData — we write raw SQL migrations.
target_metadata = None


# ── Offline mode (generates SQL script, no live DB connection) ────────────────
def run_migrations_offline() -> None:
    """
    Run migrations without a live DB connection.

    Useful for generating a migration SQL script to review or apply manually:
        uv run alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (applies migrations to the live database) ────────────────────
def run_migrations_online() -> None:
    """
    Run migrations against a live database connection using psycopg2.

    NullPool is used so each `alembic` CLI invocation gets a fresh connection
    and doesn't leave idle connections open after finishing.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare column types strictly so Alembic detects type changes.
            compare_type=True,
            # Include schemas other than public if you add them later.
            include_schemas=False,
            # Render AS INTEGER instead of AS INT for cleaner SQL output.
            render_as_batch=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()