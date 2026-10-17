"""google calendar import

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 22:15:10.366154
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "external_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("granted_scopes", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("provider IN ('google', 'microsoft')", name=op.f("ck_external_accounts_provider")),
        sa.CheckConstraint("status IN ('active', 'needs_reauth')", name=op.f("ck_external_accounts_status")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_external_accounts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_accounts")),
        sa.UniqueConstraint(
            "provider", "provider_subject", name=op.f("uq_external_accounts_provider_provider_subject")
        ),
    )
    op.create_table(
        "calendar_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("external_account_id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=True),
        sa.Column("provider_calendar_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("access_role", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("sync_mode", sa.Text(), server_default="read_only", nullable=False),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        sa.Column("sync_state", sa.Text(), server_default="idle", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "sync_mode IN ('read_only', 'two_way')", name=op.f("ck_calendar_sources_sync_mode")
        ),
        sa.CheckConstraint(
            "sync_state IN ('idle', 'syncing', 'ok', 'error', 'needs_reauth')",
            name=op.f("ck_calendar_sources_sync_state"),
        ),
        sa.ForeignKeyConstraint(
            ["external_account_id"],
            ["external_accounts.id"],
            name=op.f("fk_calendar_sources_external_account_id_external_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_calendar_sources_space_id_spaces"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_sources")),
        sa.UniqueConstraint(
            "external_account_id",
            "provider_calendar_id",
            name=op.f("uq_calendar_sources_external_account_id_provider_calendar_id"),
        ),
    )
    op.create_table(
        "event_mappings",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("calendar_source_id", sa.String(length=36), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_synced_revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["calendar_source_id"],
            ["calendar_sources.id"],
            name=op.f("fk_event_mappings_calendar_source_id_calendar_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_mappings_event_id_events"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_mappings")),
        sa.UniqueConstraint(
            "calendar_source_id",
            "provider_event_id",
            name=op.f("uq_event_mappings_calendar_source_id_provider_event_id"),
        ),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("calendar_source_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_events_calendar_source_id"), ["calendar_source_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_events_calendar_source_id_calendar_sources"),
            "calendar_sources",
            ["calendar_source_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_events_calendar_source_id_calendar_sources"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_events_calendar_source_id"))
        batch_op.drop_column("calendar_source_id")

    op.drop_table("event_mappings")
    op.drop_table("calendar_sources")
    op.drop_table("external_accounts")
    op.drop_table("app_settings")
