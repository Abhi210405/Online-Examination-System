from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint


db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False, unique=True, index=True)
    password_hash = db.Column("password", db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    roll_number = db.Column(db.String(50))
    department = db.Column(db.String(100))
    stream = db.Column(db.String(120))
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    exams = db.relationship("Exam", back_populates="creator", lazy="select")
    notifications = db.relationship(
        "Notification", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'teacher', 'student')", name="ck_users_role"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
    )


class Exam(db.Model):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    department = db.Column(db.String(120))
    stream = db.Column(db.String(120))
    topics = db.Column(db.Text)
    target_question_count = db.Column(db.Integer)
    description = db.Column(db.Text)
    duration_minutes = db.Column(db.Integer, nullable=False)
    total_marks = db.Column(db.Numeric(8, 2), nullable=False)
    negative_marks_per_wrong = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    randomize_questions = db.Column(db.Boolean, nullable=False, default=False)
    randomize_options = db.Column(db.Boolean, nullable=False, default=False)
    start_at = db.Column(db.DateTime)
    end_at = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="published")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    creator = db.relationship("User", back_populates="exams")
    questions = db.relationship(
        "Question", back_populates="exam", cascade="all, delete-orphan", order_by="Question.id"
    )
    attempts = db.relationship("Attempt", back_populates="exam", lazy="select")

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="ck_exams_duration"),
        CheckConstraint("total_marks > 0", name="ck_exams_total_marks"),
        CheckConstraint("negative_marks_per_wrong >= 0", name="ck_exams_negative_marks"),
        CheckConstraint("status IN ('draft', 'published', 'closed')", name="ck_exams_status"),
    )


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    marks = db.Column(db.Numeric(6, 2), nullable=False, default=1)
    question_type = db.Column(db.String(20), nullable=False, default="mcq")

    exam = db.relationship("Exam", back_populates="questions")
    options = db.relationship(
        "Option", back_populates="question", cascade="all, delete-orphan", order_by="Option.id"
    )

    __table_args__ = (
        CheckConstraint("marks > 0", name="ck_questions_marks"),
        CheckConstraint(
            "question_type IN ('mcq', 'subjective')", name="ck_questions_type"
        ),
    )


class Option(db.Model):
    __tablename__ = "options"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    option_text = db.Column(db.Text, nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)

    question = db.relationship("Question", back_populates="options")


class Attempt(db.Model):
    __tablename__ = "attempts"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    started_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    score = db.Column(db.Numeric(8, 2), nullable=False, default=0)
    proctoring_violations = db.Column(db.Integer, nullable=False, default=0)
    submission_reason = db.Column(db.String(20), nullable=False, default="manual")

    exam = db.relationship("Exam", back_populates="attempts")
    student = db.relationship("User")
    answers = db.relationship(
        "Answer", back_populates="attempt", cascade="all, delete-orphan", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_student"),
        CheckConstraint("score >= 0", name="ck_attempts_score"),
        CheckConstraint("proctoring_violations >= 0", name="ck_attempts_proctoring_violations"),
    )


class Answer(db.Model):
    __tablename__ = "answers"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    selected_option_id = db.Column(db.Integer, db.ForeignKey("options.id"))
    response_text = db.Column(db.Text)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)
    is_marked_for_review = db.Column(db.Boolean, nullable=False, default=False)
    manual_score = db.Column(db.Numeric(6, 2))
    feedback = db.Column(db.Text)
    graded_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    graded_at = db.Column(db.DateTime)

    attempt = db.relationship("Attempt", back_populates="answers")
    question = db.relationship("Question")
    selected_option = db.relationship("Option")
    graded_by = db.relationship("User", foreign_keys=[graded_by_id])

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_attempt_question"),
        CheckConstraint(
            "manual_score IS NULL OR manual_score >= 0", name="ck_answers_manual_score"
        ),
    )


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.String(500), nullable=False)
    link = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    read_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="notifications")


class CatalogEntry(db.Model):
    __tablename__ = "catalog_entries"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_catalog_kind_name"),
        CheckConstraint(
            "kind IN ('subject', 'department', 'stream')", name="ck_catalog_kind"
        ),
    )


class SystemSetting(db.Model):
    __tablename__ = "system_settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(500), nullable=False)
    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    action = db.Column(db.String(80), nullable=False)
    target_type = db.Column(db.String(40), nullable=False)
    target_id = db.Column(db.Integer)
    summary = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    actor = db.relationship("User", foreign_keys=[actor_id])


class QuestionBankItem(db.Model):
    __tablename__ = "question_bank_items"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject = db.Column(db.String(120), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(20), nullable=False, default="mcq")
    marks = db.Column(db.Numeric(6, 2), nullable=False, default=1)
    options = db.Column(db.JSON, nullable=False, default=list)
    correct_index = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    owner = db.relationship("User")

    __table_args__ = (
        CheckConstraint("question_type IN ('mcq', 'subjective')", name="ck_bank_question_type"),
        CheckConstraint("marks > 0", name="ck_bank_question_marks"),
        CheckConstraint(
            "(question_type = 'subjective' AND correct_index IS NULL) OR "
            "(question_type = 'mcq' AND correct_index BETWEEN 0 AND 3)",
            name="ck_bank_correct_index",
        ),
    )
