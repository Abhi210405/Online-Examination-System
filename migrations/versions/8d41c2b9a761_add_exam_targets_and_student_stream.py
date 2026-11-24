"""Add exam targeting and AI generation metadata.

Revision ID: 8d41c2b9a761
Revises: eca7f2e56ee3
Create Date: 2026-11-24 14:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "8d41c2b9a761"
down_revision = "eca7f2e56ee3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stream", sa.String(length=120), nullable=True))

    with op.batch_alter_table("exams", schema=None) as batch_op:
        batch_op.add_column(sa.Column("department", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("stream", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("topics", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("target_question_count", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("exams", schema=None) as batch_op:
        batch_op.drop_column("target_question_count")
        batch_op.drop_column("topics")
        batch_op.drop_column("stream")
        batch_op.drop_column("department")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("stream")
