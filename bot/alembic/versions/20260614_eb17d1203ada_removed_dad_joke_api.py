"""Removed Dad Joke API

Revision ID: eb17d1203ada
Revises:     720720aeef03
Create Date: 2026-06-14 08:06:22.201693+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ── Revision identifiers ──────────────────────────────────────
revision: str = 'eb17d1203ada'
down_revision: str | None = '720720aeef03'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Write raw SQL with op.execute() or use op.create_table() / op.add_column()
    # for DDL that Alembic can track automatically.
    #
    # Example:
    #   op.execute("""
    #       CREATE TABLE my_table (
    #           id BIGSERIAL PRIMARY KEY
    #       );
    #   """)
    pass


def downgrade() -> None:
    # Always implement downgrade so `alembic downgrade -1` works.
    # Example:
    #   op.execute("DROP TABLE IF EXISTS my_table;")
    pass
