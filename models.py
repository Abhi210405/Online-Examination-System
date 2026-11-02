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
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    exams = db.relationship("Exam", back_populates="creator", lazy="select")

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'teacher', 'student')", name="ck_users_role"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
    )


class Exam(db.Model):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    duration_minutes = db.Column(db.Integer, nullable=False)
    total_marks = db.Column(db.Numeric(8, 2), nullable=False)
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
        CheckConstraint("status IN ('draft', 'published', 'closed')", name="ck_exams_status"),
    )


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    marks = db.Column(db.Numeric(6, 2), nullable=False, default=1)

    exam = db.relationship("Exam", back_populates="questions")
    options = db.relationship(
        "Option", back_populates="question", cascade="all, delete-orphan", order_by="Option.id"
    )

    __table_args__ = (CheckConstraint("marks > 0", name="ck_questions_marks"),)


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
    started_at = db.Column(db.DateTime, nullable=False)
    deadline_at = db.Column(db.DateTime, nullable=False)
    submitted_at = db.Column(db.DateTime)
    score = db.Column(db.Numeric(8, 2), nullable=False, default=0)

    exam = db.relationship("Exam", back_populates="attempts")
    student = db.relationship("User")
    answers = db.relationship(
        "Answer", back_populates="attempt", cascade="all, delete-orphan", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_student"),
        CheckConstraint("score >= 0", name="ck_attempts_score"),
    )


class Answer(db.Model):
    __tablename__ = "answers"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    selected_option_id = db.Column(db.Integer, db.ForeignKey("options.id"))
    is_correct = db.Column(db.Boolean, nullable=False, default=False)

    attempt = db.relationship("Attempt", back_populates="answers")
    question = db.relationship("Question")
    selected_option = db.relationship("Option")

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_attempt_question"),
    )
