"""Moderation Updated

Revision ID: 441c8fc95c66
Revises:     437852d047e8
Create Date: 2026-07-03 08:30:28.647950+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ── Revision identifiers ──────────────────────────────────────
revision: str = '441c8fc95c66'
down_revision: str | None = '437852d047e8'
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
