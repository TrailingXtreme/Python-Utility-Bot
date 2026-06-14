"""First Migration

Maps every MongoDB collection from the nextcord bot to a PostgreSQL table.

MongoDB collection  →  PostgreSQL table
──────────────────────────────────────────────────────────────
config              →  guild_config
blacklisted_users   →  blacklisted_users
afk_user_db         →  afk_status
votes               →  bot_votes
users (votes only)  →  vote_records
suggestion_db       →  suggestion_settings
suggestion_status_db→  suggestions
invites             →  invite_tracking

Revision ID: 720720aeef03
Revises:     None
Create Date: 2026-06-13 07:28:21.689020+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ── Revision identifiers ──────────────────────────────────────
revision: str = '720720aeef03'
down_revision: str | None = None
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
