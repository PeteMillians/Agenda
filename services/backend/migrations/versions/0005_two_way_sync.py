"""two-way sync: pending pushes and sync conflicts

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23 00:30:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("needs_push", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )
    op.create_table(
        "sync_conflicts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("calendar_source_id", sa.String(length=36), nullable=False),
        sa.Column("local_snapshot", sa.Text(), nullable=False),
        sa.Column("remote_snapshot", sa.Text(), nullable=True),
        sa.Column("local_revision", sa.Integer(), nullable=False),
        sa.Column("remote_version", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.String(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'superseded')", name=op.f("ck_sync_conflicts_status")
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('keep_local', 'keep_remote', 'keep_both')",
            name=op.f("ck_sync_conflicts_resolution"),
        ),
        sa.ForeignKeyConstraint(
            ["calendar_source_id"],
            ["calendar_sources.id"],
            name=op.f("fk_sync_conflicts_calendar_source_id_calendar_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_sync_conflicts_event_id_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_conflicts")),
    )
    op.create_index(
        "uq_sync_conflicts_open_event",
        "sync_conflicts",
        ["event_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_conflicts_open_event", table_name="sync_conflicts")
    op.drop_table("sync_conflicts")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("needs_push")
