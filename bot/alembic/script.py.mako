"""${message}

Revision ID: ${up_revision}
Revises:     ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# ── Revision identifiers ──────────────────────────────────────
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


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
