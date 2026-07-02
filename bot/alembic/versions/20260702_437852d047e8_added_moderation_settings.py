"""Added Moderation Settings

Revision ID: 437852d047e8
Revises:     eb17d1203ada
Create Date: 2026-07-02 15:42:07.593074+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ── Revision identifiers ──────────────────────────────────────
revision: str = '437852d047e8'
down_revision: str | None = 'eb17d1203ada'
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
