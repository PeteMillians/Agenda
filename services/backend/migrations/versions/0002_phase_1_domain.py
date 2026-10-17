"""phase 1 domain

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 20:13:05.954965
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.String(length=36), nullable=False),
        sa.Column("timestamp", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.String(), nullable=False),
        sa.Column("lease_until", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("last_error_category", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued', 'leased', 'succeeded', 'dead_letter', 'cancelled')",
            name=op.f("ck_jobs_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_claim", ["state", "available_at"], unique=False)
        batch_op.create_index(
            "uq_jobs_idempotency",
            ["kind", "idempotency_key"],
            unique=True,
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )

    op.create_table(
        "outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("processed_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.create_index(
            "ix_outbox_pending", ["created_at"], unique=False, sqlite_where=sa.text("processed_at IS NULL")
        )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("app_lock_hash", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_table(
        "spaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("type IN ('personal', 'work', 'family', 'custom')", name=op.f("ck_spaces_type")),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name=op.f("fk_spaces_owner_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spaces")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("location", sa.Text(), server_default="", nullable=False),
        sa.Column("start_at", sa.String(), nullable=True),
        sa.Column("end_at", sa.String(), nullable=True),
        sa.Column("start_date", sa.String(), nullable=True),
        sa.Column("end_date", sa.String(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("rrule", sa.Text(), nullable=True),
        sa.Column("series_id", sa.String(length=36), nullable=True),
        sa.Column("original_start", sa.String(), nullable=True),
        sa.Column("status", sa.Text(), server_default="confirmed", nullable=False),
        sa.Column("deleted_at", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("status IN ('confirmed', 'cancelled')", name=op.f("ck_events_status")),
        sa.CheckConstraint(
            "(series_id IS NULL) = (original_start IS NULL)", name=op.f("ck_events_exception_key")
        ),
        sa.CheckConstraint(
            "(start_at IS NOT NULL AND end_at IS NOT NULL AND start_date IS NULL AND end_date IS NULL) OR (start_date IS NOT NULL AND end_date IS NOT NULL AND start_at IS NULL AND end_at IS NULL)",
            name=op.f("ck_events_timing"),
        ),
        sa.CheckConstraint(
            "NOT (series_id IS NOT NULL AND rrule IS NOT NULL)", name=op.f("ck_events_exception_no_rule")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["events.id"], name=op.f("fk_events_series_id_events"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_events_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_events_space_id_start_at"), ["space_id", "start_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_events_space_id_start_date"), ["space_id", "start_date"], unique=False
        )
        batch_op.create_index(
            "uq_events_series_original_start",
            ["series_id", "original_start"],
            unique=True,
            sqlite_where=sa.text("series_id IS NOT NULL"),
        )

    op.create_table(
        "lists",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_lists_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lists")),
    )
    with op.batch_alter_table("lists", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_lists_space_id"), ["space_id"], unique=False)

    op.create_table(
        "people",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_people_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
    )
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_people_space_id"), ["space_id"], unique=False)

    op.create_table(
        "space_memberships",
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'editor', 'viewer')", name=op.f("ck_space_memberships_role")),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_space_memberships_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_space_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("space_id", "user_id", name=op.f("pk_space_memberships")),
    )
    op.create_table(
        "list_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("checked", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"], ["lists.id"], name=op.f("fk_list_items_list_id_lists"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_list_items")),
    )
    with op.batch_alter_table("list_items", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_list_items_list_id_position"), ["list_id", "position"], unique=False
        )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("assignee_person_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("due_date", sa.String(), nullable=True),
        sa.Column("due_at", sa.String(), nullable=True),
        sa.Column("repeat_mode", sa.Text(), server_default="none", nullable=False),
        sa.Column("rrule", sa.Text(), nullable=True),
        sa.Column("repeat_interval_days", sa.Integer(), nullable=True),
        sa.Column("chain_id", sa.String(length=36), nullable=False),
        sa.Column("previous_task_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.Column("sort_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "(repeat_mode = 'none' AND rrule IS NULL AND repeat_interval_days IS NULL) OR (repeat_mode = 'schedule' AND rrule IS NOT NULL AND repeat_interval_days IS NULL     AND (due_date IS NOT NULL OR due_at IS NOT NULL)) OR (repeat_mode = 'after_completion' AND rrule IS NULL AND repeat_interval_days >= 1)",
            name=op.f("ck_tasks_repeat"),
        ),
        sa.CheckConstraint("status IN ('open', 'done', 'archived')", name=op.f("ck_tasks_status")),
        sa.CheckConstraint(
            "NOT (due_date IS NOT NULL AND due_at IS NOT NULL)", name=op.f("ck_tasks_one_due")
        ),
        sa.ForeignKeyConstraint(
            ["assignee_person_id"],
            ["people.id"],
            name=op.f("fk_tasks_assignee_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_tasks_created_by_users")),
        sa.ForeignKeyConstraint(
            ["previous_task_id"],
            ["tasks.id"],
            name=op.f("fk_tasks_previous_task_id_tasks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_tasks_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_tasks_space_id_status_sort_key"), ["space_id", "status", "sort_key"], unique=False
        )

    op.drop_table("notes")


def downgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.VARCHAR(length=36), nullable=False),
        sa.Column("body", sa.TEXT(), nullable=False),
        sa.Column("created_at", sa.VARCHAR(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_space_id_status_sort_key"))

    op.drop_table("tasks")
    with op.batch_alter_table("list_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_list_items_list_id_position"))

    op.drop_table("list_items")
    op.drop_table("space_memberships")
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_people_space_id"))

    op.drop_table("people")
    with op.batch_alter_table("lists", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_lists_space_id"))

    op.drop_table("lists")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("uq_events_series_original_start", sqlite_where=sa.text("series_id IS NOT NULL"))
        batch_op.drop_index(batch_op.f("ix_events_space_id_start_date"))
        batch_op.drop_index(batch_op.f("ix_events_space_id_start_at"))

    op.drop_table("events")
    op.drop_table("spaces")
    op.drop_table("users")
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.drop_index("ix_outbox_pending", sqlite_where=sa.text("processed_at IS NULL"))

    op.drop_table("outbox")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("uq_jobs_idempotency", sqlite_where=sa.text("idempotency_key IS NOT NULL"))
        batch_op.drop_index("ix_jobs_claim")

    op.drop_table("jobs")
    op.drop_table("audit_log")
