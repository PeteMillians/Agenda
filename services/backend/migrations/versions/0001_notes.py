"""Phase 0 notes table

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notes")
