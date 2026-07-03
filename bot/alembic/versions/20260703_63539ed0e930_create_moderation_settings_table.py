"""create moderation_settings table

Revision ID: 63539ed0e930
Revises:     441c8fc95c66
Create Date: 2026-07-03 08:39:49.912783+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# ── Revision identifiers ──────────────────────────────────────
revision: str = '63539ed0e930'
down_revision: str | None = '441c8fc95c66'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE moderation_settings (
            guild_id                BIGINT PRIMARY KEY CHECK (guild_id > 0),
            mod_log_channel_id      BIGINT CHECK (mod_log_channel_id > 0),
            kick_log_channel_id     BIGINT CHECK (kick_log_channel_id > 0),
            ban_log_channel_id      BIGINT CHECK (ban_log_channel_id > 0),
            timeout_log_channel_id  BIGINT CHECK (timeout_log_channel_id > 0),
            automod_log_channel_id  BIGINT CHECK (automod_log_channel_id > 0),
            muted_role_id           BIGINT CHECK (muted_role_id > 0),
            dm_on_punishment        BOOLEAN NOT NULL DEFAULT TRUE,
            case_count              INTEGER NOT NULL DEFAULT 0 CHECK (case_count >= 0),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS moderation_settings;")