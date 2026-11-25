"""Add dashboard workflows and richer exam responses.

Revision ID: 7ab10c24e6d3
Revises: 3f8a2c6d9e10
Create Date: 2026-11-24 17:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "7ab10c24e6d3"
down_revision = "3f8a2c6d9e10"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("exams") as batch_op:
        batch_op.add_column(
            sa.Column(
                "negative_marks_per_wrong",
                sa.Numeric(precision=6, scale=2),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "randomize_questions",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "randomize_options",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_exams_negative_marks", "negative_marks_per_wrong >= 0"
        )

    with op.batch_alter_table("questions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "question_type",
                sa.String(length=20),
                server_default="mcq",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_questions_type", "question_type IN ('mcq', 'subjective')"
        )

    with op.batch_alter_table("answers") as batch_op:
        batch_op.add_column(sa.Column("response_text", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_marked_for_review",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("manual_score", sa.Numeric(precision=6, scale=2), nullable=True)
        )
        batch_op.add_column(sa.Column("feedback", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("graded_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("graded_at", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            "fk_answers_graded_by",
            "users",
            ["graded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_answers_manual_score",
            "manual_score IS NULL OR manual_score >= 0",
        )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("link", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])

    op.create_table(
        "catalog_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('subject', 'department', 'stream')", name="ck_catalog_kind"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "name", name="uq_catalog_kind_name"),
    )

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_logs_created_at", "activity_logs", ["created_at"])

    op.create_table(
        "question_bank_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=120), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(length=20), nullable=False),
        sa.Column("marks", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column(
            "options",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("correct_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "question_type IN ('mcq', 'subjective')", name="ck_bank_question_type"
        ),
        sa.CheckConstraint("marks > 0", name="ck_bank_question_marks"),
        sa.CheckConstraint(
            "(question_type = 'subjective' AND correct_index IS NULL) OR "
            "(question_type = 'mcq' AND correct_index BETWEEN 0 AND 3)",
            name="ck_bank_correct_index",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_question_bank_items_owner_id", "question_bank_items", ["owner_id"])


def downgrade():
    op.drop_index("ix_question_bank_items_owner_id", table_name="question_bank_items")
    op.drop_table("question_bank_items")
    op.drop_index("ix_activity_logs_created_at", table_name="activity_logs")
    op.drop_table("activity_logs")
    op.drop_table("system_settings")
    op.drop_table("catalog_entries")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    with op.batch_alter_table("answers") as batch_op:
        batch_op.drop_constraint("ck_answers_manual_score", type_="check")
        batch_op.drop_constraint("fk_answers_graded_by", type_="foreignkey")
        batch_op.drop_column("graded_at")
        batch_op.drop_column("graded_by_id")
        batch_op.drop_column("feedback")
        batch_op.drop_column("manual_score")
        batch_op.drop_column("is_marked_for_review")
        batch_op.drop_column("response_text")

    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_constraint("ck_questions_type", type_="check")
        batch_op.drop_column("question_type")

    with op.batch_alter_table("exams") as batch_op:
        batch_op.drop_constraint("ck_exams_negative_marks", type_="check")
        batch_op.drop_column("randomize_options")
        batch_op.drop_column("randomize_questions")
        batch_op.drop_column("negative_marks_per_wrong")
