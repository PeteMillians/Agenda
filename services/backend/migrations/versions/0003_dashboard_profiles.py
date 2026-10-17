"""dashboard profiles

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 21:45:14.507401
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("space_ids", sa.Text(), server_default="[]", nullable=False),
        sa.Column("layout_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_dashboard_profiles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_profiles")),
    )
    with op.batch_alter_table("dashboard_profiles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_dashboard_profiles_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("dashboard_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dashboard_profiles_user_id"))

    op.drop_table("dashboard_profiles")
