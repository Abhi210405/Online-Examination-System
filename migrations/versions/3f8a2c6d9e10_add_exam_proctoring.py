"""Add exam proctoring state.

Revision ID: 3f8a2c6d9e10
Revises: 8d41c2b9a761
Create Date: 2026-11-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "3f8a2c6d9e10"
down_revision = "8d41c2b9a761"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.alter_column("started_at", existing_type=sa.DateTime(), nullable=True)
        batch_op.alter_column("deadline_at", existing_type=sa.DateTime(), nullable=True)
        batch_op.add_column(
            sa.Column(
                "proctoring_violations",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "submission_reason",
                sa.String(length=20),
                server_default="manual",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_attempts_proctoring_violations", "proctoring_violations >= 0"
        )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE attempts SET started_at = CURRENT_TIMESTAMP "
            "WHERE started_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE attempts SET deadline_at = CURRENT_TIMESTAMP "
            "WHERE deadline_at IS NULL"
        )
    )
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_attempts_proctoring_violations", type_="check")
        batch_op.drop_column("submission_reason")
        batch_op.drop_column("proctoring_violations")
        batch_op.alter_column("started_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column("deadline_at", existing_type=sa.DateTime(), nullable=False)
