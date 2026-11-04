import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_migrate import Migrate, upgrade as run_migrations
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
import bcrypt

from models import Answer, Attempt, Exam, Option, Question, User, db


load_dotenv()


def _is_production(environ=None):
    environ = os.environ if environ is None else environ
    app_env = environ.get("APP_ENV", "").lower()
    vercel_env = environ.get("VERCEL_ENV", "production").lower()
    is_vercel_production = environ.get("VERCEL") == "1" and vercel_env != "development"
    return app_env == "production" or is_vercel_production


def _normalize_database_url(database_url):
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://") :]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    if database_url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+psycopg2://") :]
    return database_url


def _database_configuration(instance_path, environ=None):
    environ = os.environ if environ is None else environ
    production = _is_production(environ)
    configured_url = environ.get("DATABASE_URL")
    if production and not configured_url:
        raise RuntimeError("Set DATABASE_URL to a persistent PostgreSQL connection string in production.")

    database_url = configured_url or f"sqlite:///{Path(instance_path) / 'college_exam_system.sqlite3'}"
    database_url = _normalize_database_url(database_url)
    parsed_url = make_url(database_url)
    is_postgresql = parsed_url.get_backend_name() == "postgresql"
    if production and not is_postgresql:
        raise RuntimeError("Production requires a persistent PostgreSQL DATABASE_URL; SQLite is not supported.")
    if is_postgresql and parsed_url.drivername != "postgresql+psycopg":
        raise RuntimeError("Use a PostgreSQL URL supported by the configured Psycopg 3 driver.")

    if parsed_url.get_backend_name() == "sqlite":
        sqlite_path = parsed_url.database
        if sqlite_path and sqlite_path != ":memory:" and not sqlite_path.startswith("file:"):
            resolved_path = Path(sqlite_path)
            if not resolved_path.is_absolute():
                resolved_path = Path(instance_path) / resolved_path
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = parsed_url.set(database=str(resolved_path)).render_as_string(
                hide_password=False
            )

    engine_options = {"pool_pre_ping": True}
    if is_postgresql:
        engine_options.update(
            pool_recycle=300,
            pool_size=1,
            max_overflow=0,
            pool_timeout=10,
            connect_args={"connect_timeout": 10},
        )
    trusted_hosts = environ.get("TRUSTED_HOSTS")
    if trusted_hosts:
        trusted_hosts = [host.strip() for host in trusted_hosts.split(",") if host.strip()]
    elif production:
        trusted_hosts = ["*.vercel.app"]
    else:
        trusted_hosts = None

    return {
        "production": production,
        "database_url": database_url,
        "engine_options": engine_options,
        "trusted_hosts": trusted_hosts,
    }


def _secret_key(instance_path, production=False):
    configured = os.environ.get("SECRET_KEY")
    if production:
        if not configured or len(configured) < 32:
            raise RuntimeError(
                "Set a private SECRET_KEY of at least 32 characters in production."
            )
        return configured
    if configured:
        return configured
    secret_file = Path(instance_path) / "secret_key"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return secret_file.read_text(encoding="utf-8").strip()


app = Flask(__name__, instance_relative_config=True, static_folder=None)
database_config = _database_configuration(app.instance_path)
if not database_config["production"]:
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
app.config.update(
    SECRET_KEY=_secret_key(app.instance_path, database_config["production"]),
    SQLALCHEMY_DATABASE_URI=database_config["database_url"],
    SQLALCHEMY_ENGINE_OPTIONS=database_config["engine_options"],
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=database_config["production"],
    TRUSTED_HOSTS=database_config["trusted_hosts"],
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
db.init_app(app)
migrate = Migrate(app, db, compare_type=True)


@app.get("/static/<path:filename>", endpoint="static")
def static_assets(filename):
    return send_from_directory(Path(app.root_path) / "public" / "static", filename)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(connection, _record):
    if connection.__class__.__module__.startswith("sqlite3"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_user():
    return getattr(g, "user", None)


def _csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def load_user_and_check_csrf():
    user_id = session.get("user_id")
    g.user = db.session.get(User, user_id) if user_id else None
    if g.user is not None and g.user.status != "active":
        session.clear()
        g.user = None

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        submitted = request.headers.get("X-CSRF-Token")
        if not submitted:
            submitted = request.form.get("csrf_token")
        if not submitted or not secrets.compare_digest(submitted, session.get("_csrf_token", "")):
            abort(400, description="Your session token is invalid or expired. Refresh and try again.")


@app.context_processor
def inject_template_helpers():
    return {
        "current_user": current_user,
        "csrf_token": _csrf_token,
        "utcnow": utcnow,
        "unix_timestamp": lambda value: int(value.timestamp()) if value.tzinfo else int(
            (value - datetime(1970, 1, 1)).total_seconds()
        ),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user().role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def _dashboard_for(user):
    endpoints = {
        "admin": "admin_dashboard",
        "teacher": "teacher_dashboard",
        "student": "student_dashboard",
    }
    return url_for(endpoints[user.role])


def _parse_datetime(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _exam_access(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    user = current_user()
    if user.role == "teacher" and exam.created_by != user.id:
        abort(403)
    return exam


def _active_attempt(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.student_id != current_user().id:
        abort(403)
    return attempt


def _exam_open(exam, now=None):
    now = now or utcnow()
    return (
        exam.status == "published"
        and (exam.start_at is None or exam.start_at <= now)
        and (exam.end_at is None or exam.end_at >= now)
    )


def _save_answer(attempt, question_id, selected_option_id):
    question = db.session.get(Question, question_id)
    if question is None or question.exam_id != attempt.exam_id:
        abort(400, description="That question does not belong to this examination.")

    answer = db.session.scalar(
        select(Answer).where(
            Answer.attempt_id == attempt.id,
            Answer.question_id == question.id,
        )
    )
    if selected_option_id is None:
        if answer:
            db.session.delete(answer)
        return

    option = db.session.get(Option, selected_option_id)
    if option is None or option.question_id != question.id:
        abort(400, description="That answer choice is not valid for this question.")
    if answer is None:
        answer = Answer(attempt=attempt, question=question)
        db.session.add(answer)
    answer.selected_option = option
    answer.is_correct = False


def _submit_attempt(attempt, final_answers=None):
    if attempt.submitted_at is not None:
        return False
    now = utcnow()
    if now < attempt.deadline_at and final_answers is not None:
        for raw_question_id, raw_option_id in final_answers.items():
            try:
                question_id = int(raw_question_id)
                option_id = int(raw_option_id) if raw_option_id else None
            except (TypeError, ValueError):
                abort(400, description="An answer contains an invalid question or option.")
            _save_answer(attempt, question_id, option_id)

    answers_by_question = {answer.question_id: answer for answer in attempt.answers}
    for question in attempt.exam.questions:
        if question.id not in answers_by_question:
            answer = Answer(attempt=attempt, question=question)
            db.session.add(answer)
            answers_by_question[question.id] = answer

    total = Decimal("0")
    for answer in answers_by_question.values():
        correct = (
            answer.selected_option is not None
            and answer.selected_option.question_id == answer.question_id
            and answer.selected_option.is_correct
        )
        answer.is_correct = bool(correct)
        if correct:
            total += Decimal(answer.question.marks)

    attempt.score = min(total, Decimal(attempt.exam.total_marks))
    attempt.submitted_at = now
    db.session.commit()
    return True


@app.route("/")
def home():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    published_count = db.session.scalar(
        select(func.count(Exam.id)).where(Exam.status == "published")
    ) or 0
    return render_template("home.html", published_count=published_count)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        roll_number = request.form.get("roll_number", "").strip()
        department = request.form.get("department", "").strip()
        if not name or "@" not in email or len(password) < 8 or not roll_number or not department:
            flash("Enter all required details and use a password of at least 8 characters.", "error")
        else:
            db.session.add(
                User(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role="student",
                    roll_number=roll_number,
                    department=department,
                )
            )
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email address is already registered.", "error")
            else:
                flash("Your student account is ready. Please sign in.")
                return redirect(url_for("login"))
    return render_template("auth_form.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(func.lower(User.email) == email))
        password_matches = False
        if user and user.status == "active":
            stored_hash = user.password_hash
            if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
                password_matches = bcrypt.checkpw(password.encode()[:72], stored_hash.encode())
            else:
                password_matches = check_password_hash(stored_hash, password)
        if password_matches:
            if user.password_hash.startswith(("$2a$", "$2b$", "$2y$")):
                user.password_hash = generate_password_hash(password)
                db.session.commit()
            session.clear()
            session["user_id"] = user.id
            _csrf_token()
            return redirect(_dashboard_for(user))
        flash("Invalid email or password, or this account is inactive.", "error")
    return render_template("auth_form.html", mode="login")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been signed out.")
    return redirect(url_for("home"))


@app.route("/setup/first-admin", methods=["GET", "POST"])
def first_admin():
    if _is_production():
        abort(404)
    if db.session.scalar(select(func.count(User.id)).where(User.role == "admin")):
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or "@" not in email or len(password) < 10:
            flash("Enter a valid name and email, and use a password of at least 10 characters.", "error")
        else:
            db.session.add(
                User(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role="admin",
                )
            )
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email address is already registered.", "error")
            else:
                flash("Administrator created. Sign in to manage the portal.")
                return redirect(url_for("login"))
    return render_template("auth_form.html", mode="first_admin")


@app.get("/dashboard")
@login_required
def dashboard():
    return redirect(_dashboard_for(current_user()))


@app.get("/admin")
@roles_required("admin")
def admin_dashboard():
    counts = {
        "users": db.session.scalar(select(func.count(User.id))) or 0,
        "exams": db.session.scalar(select(func.count(Exam.id))) or 0,
        "questions": db.session.scalar(select(func.count(Question.id))) or 0,
        "attempts": db.session.scalar(select(func.count(Attempt.id))) or 0,
    }
    recent_exams = db.session.scalars(select(Exam).order_by(Exam.created_at.desc()).limit(6)).all()
    return render_template("admin_dashboard.html", counts=counts, recent_exams=recent_exams)


@app.route("/admin/users", methods=["GET", "POST"])
@roles_required("admin")
def admin_users():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if not name or "@" not in email or len(password) < 8 or role not in {"admin", "teacher"}:
            flash("Enter valid account details and choose an administrator or teacher role.", "error")
        else:
            db.session.add(
                User(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role=role,
                )
            )
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email address is already registered.", "error")
            else:
                flash(f"{role.title()} account created.")
                return redirect(url_for("admin_users"))
    users = db.session.scalars(select(User).order_by(User.created_at.desc())).all()
    return render_template("admin_users.html", users=users)


@app.post("/admin/users/<int:user_id>/status")
@roles_required("admin")
def update_user_status(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user().id:
        flash("You cannot deactivate your own administrator account.", "error")
    else:
        user.status = "inactive" if user.status == "active" else "active"
        db.session.commit()
        flash(f"{user.name}'s account is now {user.status}.")
    return redirect(url_for("admin_users"))


@app.get("/teacher")
@roles_required("teacher", "admin")
def teacher_dashboard():
    user = current_user()
    exams_query = select(Exam).order_by(Exam.created_at.desc())
    if user.role == "teacher":
        exams_query = exams_query.where(Exam.created_by == user.id)
    exams = db.session.scalars(exams_query).all()
    return render_template(
        "teacher_dashboard.html",
        exams=exams,
        attempt_count=sum(len(exam.attempts) for exam in exams),
    )


@app.route("/teacher/exams/new", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def create_exam():
    if request.method == "POST":
        try:
            duration = int(request.form.get("duration_minutes", ""))
            total_marks = Decimal(request.form.get("total_marks", ""))
            start_at = _parse_datetime(
                request.form.get("start_at") or request.form.get("start_at_local", "")
            )
            end_at = _parse_datetime(
                request.form.get("end_at") or request.form.get("end_at_local", "")
            )
        except (ValueError, InvalidOperation):
            flash("Enter a valid duration, total marks, and exam schedule.", "error")
        else:
            title = request.form.get("title", "").strip()
            subject = request.form.get("subject", "").strip()
            if (
                not title
                or not subject
                or not 1 <= duration <= 10080
                or not total_marks.is_finite()
                or not Decimal("0") < total_marks <= Decimal("999999.99")
                or (start_at and end_at and end_at <= start_at)
            ):
                flash("Enter valid exam details. The end time must be after the start time.", "error")
            else:
                exam = Exam(
                    title=title,
                    subject=subject,
                    description=request.form.get("description", "").strip(),
                    duration_minutes=duration,
                    total_marks=total_marks,
                    start_at=start_at,
                    end_at=end_at,
                    created_by=current_user().id,
                    status=request.form.get("status", "published"),
                )
                if exam.status not in {"draft", "published", "closed"}:
                    abort(400)
                db.session.add(exam)
                db.session.commit()
                flash("Examination created. Add its questions before students begin.")
                return redirect(url_for("manage_questions", exam_id=exam.id))
    return render_template("exam_form.html", exam=None)


@app.route("/teacher/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def edit_exam(exam_id):
    exam = _exam_access(exam_id)
    if exam.attempts:
        flash("This examination is locked because a student has already started it.", "error")
        return redirect(url_for("teacher_dashboard"))
    if request.method == "POST":
        try:
            duration = int(request.form.get("duration_minutes", ""))
            total_marks = Decimal(request.form.get("total_marks", ""))
            start_at = _parse_datetime(
                request.form.get("start_at") or request.form.get("start_at_local", "")
            )
            end_at = _parse_datetime(
                request.form.get("end_at") or request.form.get("end_at_local", "")
            )
        except (ValueError, InvalidOperation):
            flash("Enter a valid duration, total marks, and exam schedule.", "error")
        else:
            title = request.form.get("title", "").strip()
            subject = request.form.get("subject", "").strip()
            status = request.form.get("status", "")
            if (
                not title
                or not subject
                or not 1 <= duration <= 10080
                or not total_marks.is_finite()
                or not Decimal("0") < total_marks <= Decimal("999999.99")
                or status not in {"draft", "published", "closed"}
                or (start_at and end_at and end_at <= start_at)
            ):
                flash("Review the exam fields and enter a valid schedule.", "error")
            else:
                exam.title = title
                exam.subject = subject
                exam.description = request.form.get("description", "").strip()
                exam.duration_minutes = duration
                exam.total_marks = total_marks
                exam.start_at = start_at
                exam.end_at = end_at
                exam.status = status
                db.session.commit()
                flash("Examination updated.")
                return redirect(url_for("teacher_dashboard"))
    return render_template("exam_form.html", exam=exam)


@app.route("/teacher/exams/<int:exam_id>/questions", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def manage_questions(exam_id):
    exam = _exam_access(exam_id)
    if request.method == "POST":
        if exam.attempts:
            flash("Questions are locked because a student has already started this examination.", "error")
            return redirect(url_for("manage_questions", exam_id=exam.id))
        question_text = request.form.get("question_text", "").strip()
        options = [item.strip() for item in request.form.getlist("options")]
        try:
            marks = Decimal(request.form.get("marks", "1"))
            correct_index = int(request.form.get("correct_index", ""))
        except (ValueError, InvalidOperation):
            flash("Enter valid marks and select the correct answer.", "error")
        else:
            if (
                not question_text
                or len(options) != 4
                or any(not option for option in options)
                or not marks.is_finite()
                or not Decimal("0") < marks <= Decimal("9999.99")
                or correct_index not in range(4)
            ):
                flash("Provide a question, four non-empty choices, positive marks, and one correct choice.", "error")
            else:
                question = Question(exam=exam, question_text=question_text, marks=marks)
                question.options = [
                    Option(option_text=text, is_correct=index == correct_index)
                    for index, text in enumerate(options)
                ]
                db.session.add(question)
                db.session.commit()
                flash("Question added.")
                return redirect(url_for("manage_questions", exam_id=exam.id))
    return render_template("questions.html", exam=exam, questions_locked=bool(exam.attempts))


@app.post("/teacher/questions/<int:question_id>/delete")
@roles_required("teacher", "admin")
def delete_question(question_id):
    question = db.get_or_404(Question, question_id)
    exam = _exam_access(question.exam_id)
    if exam.attempts:
        flash("Questions cannot be removed after a student has started this examination.", "error")
    else:
        db.session.delete(question)
        db.session.commit()
        flash("Question removed.")
    return redirect(url_for("manage_questions", exam_id=exam.id))


@app.get("/teacher/results")
@roles_required("teacher", "admin")
def teacher_results():
    query = select(Attempt).join(Exam).where(Attempt.submitted_at.is_not(None))
    if current_user().role == "teacher":
        query = query.where(Exam.created_by == current_user().id)
    attempts = db.session.scalars(query.order_by(Attempt.submitted_at.desc())).all()
    return render_template("teacher_results.html", attempts=attempts)


@app.get("/student")
@roles_required("student")
def student_dashboard():
    exams = db.session.scalars(
        select(Exam).where(Exam.status == "published").order_by(Exam.created_at.desc())
    ).all()
    attempts = {
        attempt.exam_id: attempt
        for attempt in db.session.scalars(
            select(Attempt).where(Attempt.student_id == current_user().id)
        ).all()
    }
    now = utcnow()
    return render_template("student_dashboard.html", exams=exams, attempts=attempts, now=now)


@app.get("/student/exams/<int:exam_id>/instructions")
@roles_required("student")
def exam_instructions(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    if exam.status != "published":
        abort(404)
    attempt = db.session.scalar(
        select(Attempt).where(
            Attempt.exam_id == exam.id,
            Attempt.student_id == current_user().id,
        )
    )
    return render_template("exam_instructions.html", exam=exam, attempt=attempt, now=utcnow())


@app.post("/student/exams/<int:exam_id>/start")
@roles_required("student")
def start_exam(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    if not _exam_open(exam):
        flash("This examination is not currently open.", "error")
        return redirect(url_for("student_dashboard"))
    if not exam.questions:
        flash("This examination does not have any questions yet.", "error")
        return redirect(url_for("student_dashboard"))
    attempt = db.session.scalar(
        select(Attempt).where(
            Attempt.exam_id == exam.id,
            Attempt.student_id == current_user().id,
        )
    )
    if attempt is not None:
        if attempt.submitted_at is None and utcnow() >= attempt.deadline_at:
            _submit_attempt(attempt)
            flash("Your examination time has ended and the saved answers were submitted.")
            return redirect(url_for("student_result", attempt_id=attempt.id))
        if attempt.submitted_at is not None:
            flash("You have already submitted this examination.", "error")
            return redirect(url_for("student_result", attempt_id=attempt.id))
        return redirect(url_for("take_exam", attempt_id=attempt.id))

    now = utcnow()
    attempt = Attempt(
        exam=exam,
        student=current_user(),
        started_at=now,
        deadline_at=now + timedelta(minutes=exam.duration_minutes),
    )
    db.session.add(attempt)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        attempt = db.session.scalar(
            select(Attempt).where(
                Attempt.exam_id == exam.id,
                Attempt.student_id == current_user().id,
            )
        )
        if attempt is None:
            raise
    return redirect(url_for("take_exam", attempt_id=attempt.id))


@app.get("/student/attempts/<int:attempt_id>")
@roles_required("student")
def take_exam(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return redirect(url_for("student_result", attempt_id=attempt.id))
    if utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt)
        flash("Time expired. Your saved answers have been submitted.")
        return redirect(url_for("student_result", attempt_id=attempt.id))
    saved = {answer.question_id: answer.selected_option_id for answer in attempt.answers}
    return render_template("take_exam.html", attempt=attempt, saved=saved)


@app.post("/api/attempts/<int:attempt_id>/answers")
@roles_required("student")
def autosave_answer(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return jsonify(error="This examination has already been submitted."), 409
    if utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt)
        return jsonify(status="submitted", redirect=url_for("student_result", attempt_id=attempt.id))
    payload = request.get_json(silent=False)
    try:
        question_id = int(payload["question_id"])
        raw_option_id = payload.get("option_id")
        option_id = int(raw_option_id) if raw_option_id is not None else None
    except (TypeError, ValueError, KeyError):
        abort(400, description="Provide a valid question_id and option_id.")
    _save_answer(attempt, question_id, option_id)
    db.session.commit()
    return jsonify(status="saved", saved_at=utcnow().isoformat())


@app.post("/student/attempts/<int:attempt_id>/submit")
@roles_required("student")
def submit_exam(attempt_id):
    attempt = _active_attempt(attempt_id)
    final_answers = {
        key.removeprefix("q_"): value
        for key, value in request.form.items()
        if key.startswith("q_")
    }
    submitted = _submit_attempt(attempt, final_answers)
    if not submitted:
        flash("Your examination has already been submitted.")
    else:
        flash("Your examination has been submitted.")
    return redirect(url_for("student_result", attempt_id=attempt.id))


@app.get("/student/results")
@roles_required("student")
def student_results():
    attempts = db.session.scalars(
        select(Attempt)
        .where(Attempt.student_id == current_user().id, Attempt.submitted_at.is_not(None))
        .order_by(Attempt.submitted_at.desc())
    ).all()
    return render_template("student_results.html", attempts=attempts, selected=None)


@app.get("/student/results/<int:attempt_id>")
@roles_required("student")
def student_result(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is None:
        return redirect(url_for("take_exam", attempt_id=attempt.id))
    return render_template("student_results.html", attempts=[attempt], selected=attempt)


@app.cli.command("init-db")
def init_db_command():
    run_migrations()
    click.echo("Database migrations are up to date.")


@app.cli.command("create-admin")
@click.option("--name", prompt=True)
@click.option("--email", prompt=True)
@click.password_option()
def create_admin_command(name, email, password):
    if db.session.scalar(select(func.count(User.id)).where(User.role == "admin")):
        raise click.ClickException("An administrator already exists.")
    if not name.strip() or "@" not in email or len(password) < 10:
        raise click.ClickException("Enter a valid name/email and a password of at least 10 characters.")
    db.session.add(
        User(
            name=name.strip(),
            email=email.strip().lower(),
            password_hash=generate_password_hash(password),
            role="admin",
        )
    )
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise click.ClickException("That email address is already registered.") from error
    click.echo("Administrator account created.")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def handle_http_error(error):
    return render_template("error.html", error=error), error.code


@app.teardown_appcontext
def cleanup_database_session(_error=None):
    db.session.remove()


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    )
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


if __name__ == "__main__":
    app.run()
