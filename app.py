import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
from sqlalchemy import event, func, or_, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
import bcrypt

from models import Answer, Attempt, Exam, Option, Question, User, db


load_dotenv()

PROCTORING_VIOLATION_LIMIT = 3
PROCTORING_EVENT_TYPES = {
    "tab_switch",
    "fullscreen_exit",
    "clipboard_attempt",
    "face_missing",
}


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
    try:
        parsed_url = make_url(database_url)
    except ArgumentError as error:
        if production:
            raise RuntimeError(
                "DATABASE_URL must be a complete PostgreSQL connection URI; copy the full URL from your database provider."
            ) from error
        raise
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
        trusted_hosts = [".vercel.app"]
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


def _generate_ai_questions(exam, question_count):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("AI generation is unavailable until GEMINI_API_KEY is configured.")

    prompt = {
        "department": exam.department,
        "stream": exam.stream,
        "subject": exam.subject,
        "topics": exam.topics,
        "number_of_questions": question_count,
        "question_format": {
            "question_text": "string",
            "options": ["exactly four answer strings"],
            "correct_index": "integer from 0 to 3",
        },
        "requirements": [
            "Create accurate, unambiguous multiple-choice questions suitable for the specified stream and department.",
            "Distribute questions across the supplied topics.",
            "Return only a JSON object with a questions array matching the exact requested count.",
            "Do not include explanations or markdown.",
        ],
    }
    response_schema = {
        "type": "OBJECT",
        "properties": {
            "questions": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "question_text": {"type": "STRING"},
                        "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "correct_index": {"type": "INTEGER"},
                    },
                    "required": ["question_text", "options", "correct_index"],
                },
            }
        },
        "required": ["questions"],
    }
    request_data = json.dumps(
        {
            "systemInstruction": {
                "parts": [
                    {"text": "You create high-quality educational multiple-choice assessment questions."}
                ]
            },
            "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt)}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }
    ).encode("utf-8")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    api_request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=request_data,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(api_request, timeout=45) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                "Gemini rate limit or API quota reached. Check Google AI Studio quota/billing and try again."
            ) from error
        if error.code in {401, 403}:
            raise RuntimeError(
                "Gemini rejected the API key or its access. Check the key and Gemini API access in Google AI Studio."
            ) from error
        raise RuntimeError(f"Gemini rejected the request (HTTP {error.code}). Check the API key and model.") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError("Could not connect to Gemini. Please try again later.") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Gemini returned an invalid response. Please try again.") from error

    try:
        parts = response_data["candidates"][0]["content"]["parts"]
        content = "".join(part["text"] for part in parts if isinstance(part.get("text"), str))
        result = json.loads(content)
        questions = result["questions"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("The AI response was not in the expected question format. Please try again.") from error

    if not isinstance(questions, list) or len(questions) != question_count:
        raise ValueError(f"The AI did not return exactly {question_count} questions. Please try again.")
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("The AI returned an invalid question. Please try again.")
        text = question.get("question_text")
        options = question.get("options")
        correct_index = question.get("correct_index")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 5000
            or not isinstance(options, list)
            or len(options) != 4
            or any(not isinstance(option, str) or not option.strip() or len(option) > 2000 for option in options)
            or type(correct_index) is not int
            or correct_index not in range(4)
        ):
            raise ValueError("The AI returned a question with invalid choices or answer data. Please try again.")
    return questions


def _student_can_access_exam(exam, student):
    department_target = (exam.department or "").strip().casefold()
    stream_target = (exam.stream or "").strip().casefold()
    if department_target not in {"", "*", "all departments"} and (
        not student.department or exam.department.strip().casefold() != student.department.strip().casefold()
    ):
        return False
    if (
        stream_target not in {"", "*", "all streams"}
        and student.stream
        and stream_target != student.stream.strip().casefold()
    ):
        return False
    return True


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


def _submit_attempt(attempt, final_answers=None, reason="manual"):
    if attempt.submitted_at is not None:
        return False
    now = utcnow()
    if reason == "manual" and attempt.deadline_at is not None and now >= attempt.deadline_at:
        reason = "time_expired"
    if attempt.deadline_at is not None and now < attempt.deadline_at and final_answers is not None:
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
    attempt.submission_reason = reason
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
        stream = request.form.get("stream", "").strip()
        if not name or "@" not in email or len(password) < 8 or not roll_number or not department or not stream:
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
                    stream=stream,
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


def _login_user(requested_role=None):
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    requested_role = request.form.get("role") or requested_role
    user = db.session.scalar(select(User).where(func.lower(User.email) == email))
    password_matches = False
    if user and user.status == "active":
        stored_hash = user.password_hash
        if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
            password_matches = bcrypt.checkpw(password.encode()[:72], stored_hash.encode())
        else:
            password_matches = check_password_hash(stored_hash, password)
    if not password_matches:
        flash("Invalid email or password, or this account is inactive.", "error")
        return None
    if requested_role and user.role != requested_role:
        flash("This account does not belong to the selected sign-in role.", "error")
        return None
    if user.password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        user.password_hash = generate_password_hash(password)
        db.session.commit()
    session.clear()
    session["user_id"] = user.id
    _csrf_token()
    return user


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    requested_role = request.args.get("role")
    if request.method == "POST":
        user = _login_user(requested_role=requested_role)
        if user is not None:
            return redirect(_dashboard_for(user))
    return render_template("auth_form.html", mode="login", selected_role=requested_role)


@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    return login_for_role("student")


@app.route("/teacher/login", methods=["GET", "POST"])
def teacher_login():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    return login_for_role("teacher")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_user():
        return redirect(_dashboard_for(current_user()))
    return login_for_role("admin")


def login_for_role(role):
    if request.method == "POST":
        user = _login_user(requested_role=role)
        if user is not None:
            return redirect(_dashboard_for(user))
    return render_template("auth_form.html", mode="login", selected_role=role)


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


@app.post("/admin/users/<int:user_id>/password")
@roles_required("admin")
def reset_user_password(user_id):
    user = db.get_or_404(User, user_id)
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("The new password must contain at least 8 characters.", "error")
    else:
        user.password_hash = generate_password_hash(password)
        user.status = "active"
        db.session.commit()
        flash(f"{user.name}'s temporary password is set to: {password}")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/delete")
@roles_required("admin")
def delete_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user().id:
        flash("You cannot delete your own administrator account.", "error")
        return redirect(url_for("admin_users"))

    created_exam_ids = select(Exam.id).where(Exam.created_by == user.id)
    affected_attempt_ids = select(Attempt.id).where(
        (Attempt.student_id == user.id) | Attempt.exam_id.in_(created_exam_ids)
    )
    db.session.execute(Answer.__table__.delete().where(Answer.attempt_id.in_(affected_attempt_ids)))
    db.session.execute(Attempt.__table__.delete().where(
        (Attempt.student_id == user.id) | Attempt.exam_id.in_(created_exam_ids)
    ))

    affected_question_ids = select(Question.id).where(Question.exam_id.in_(created_exam_ids))
    db.session.execute(
        Answer.__table__.delete().where(Answer.question_id.in_(affected_question_ids))
    )
    db.session.execute(
        Option.__table__.delete().where(Option.question_id.in_(affected_question_ids))
    )
    db.session.execute(Question.__table__.delete().where(Question.exam_id.in_(created_exam_ids)))
    db.session.execute(Exam.__table__.delete().where(Exam.created_by == user.id))
    db.session.delete(user)
    db.session.commit()
    flash(f"{user.name}'s account was deleted.")
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
            target_question_count = int(request.form.get("target_question_count", ""))
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
            department = request.form.get("department", "").strip()
            stream = request.form.get("stream", "").strip()
            topics = request.form.get("topics", "").strip()
            if (
                not title
                or not subject
                or not department
                or not stream
                or not topics
                or len(department) > 120
                or len(stream) > 120
                or len(topics) > 2000
                or not 1 <= target_question_count <= 50
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
                    department=department,
                    stream=stream,
                    topics=topics,
                    target_question_count=target_question_count,
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
            target_question_count = int(request.form.get("target_question_count", ""))
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
            department = request.form.get("department", "").strip()
            stream = request.form.get("stream", "").strip()
            topics = request.form.get("topics", "").strip()
            status = request.form.get("status", "")
            if (
                not title
                or not subject
                or not department
                or not stream
                or not topics
                or len(department) > 120
                or len(stream) > 120
                or len(topics) > 2000
                or not 1 <= target_question_count <= 50
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
                exam.department = department
                exam.stream = stream
                exam.topics = topics
                exam.target_question_count = target_question_count
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
    return render_template(
        "questions.html",
        exam=exam,
        questions_locked=bool(exam.attempts),
        ai_available=bool(os.environ.get("GEMINI_API_KEY")),
    )


@app.post("/teacher/exams/<int:exam_id>/generate")
@roles_required("teacher", "admin")
def generate_exam_questions(exam_id):
    exam = _exam_access(exam_id)
    if exam.attempts:
        flash("Questions are locked because a student has already started this examination.", "error")
        return redirect(url_for("manage_questions", exam_id=exam.id))
    try:
        question_count = int(request.form.get("question_count", str(exam.target_question_count or 0)))
    except ValueError:
        flash("Enter a valid number of questions.", "error")
        return redirect(url_for("manage_questions", exam_id=exam.id))
    if not 1 <= question_count <= 50:
        flash("Choose between 1 and 50 questions.", "error")
        return redirect(url_for("manage_questions", exam_id=exam.id))
    question_count -= len(exam.questions)
    if question_count <= 0:
        flash("This exam already has the requested number of questions.", "error")
        return redirect(url_for("manage_questions", exam_id=exam.id))

    try:
        generated_questions = _generate_ai_questions(exam, question_count)
    except (RuntimeError, ValueError) as error:
        app.logger.warning("AI question generation failed: %s", error)
        flash(str(error), "error")
        return redirect(url_for("manage_questions", exam_id=exam.id))

    existing_marks = sum((Decimal(question.marks) for question in exam.questions), Decimal("0"))
    remaining_marks = max(exam.total_marks - existing_marks, Decimal("0"))
    marks_per_question = (remaining_marks / question_count).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    if marks_per_question <= 0:
        marks_per_question = Decimal("1")
    total_question_count = len(exam.questions) + len(generated_questions)
    questions = []
    for index, generated in enumerate(generated_questions):
        marks = (
            remaining_marks - marks_per_question * (question_count - 1)
            if index == question_count - 1
            else marks_per_question
        )
        if marks <= 0:
            marks = marks_per_question
        question = Question(
            exam=exam,
            question_text=generated["question_text"].strip(),
            marks=marks,
        )
        question.options = [
            Option(option_text=text.strip(), is_correct=option_index == generated["correct_index"])
            for option_index, text in enumerate(generated["options"])
        ]
        questions.append(question)
    db.session.add_all(questions)
    exam.target_question_count = total_question_count
    exam.status = "draft"
    db.session.commit()
    flash(
        f"AI generated and added {len(questions)} questions. The exam is now a draft; review it before publishing."
    )
    return redirect(url_for("manage_questions", exam_id=exam.id))


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
    student = current_user()
    exam_query = select(Exam).where(Exam.status == "published")
    if student.department:
        exam_query = exam_query.where(
            or_(
                Exam.department.is_(None),
                func.lower(Exam.department).in_(["*", "all departments"]),
                func.lower(Exam.department) == student.department.strip().lower(),
            )
        )
    else:
        exam_query = exam_query.where(
            or_(Exam.department.is_(None), func.lower(Exam.department).in_(["*", "all departments"]))
        )
    if student.stream:
        exam_query = exam_query.where(
            or_(
                Exam.stream.is_(None),
                func.lower(Exam.stream).in_(["*", "all streams"]),
                func.lower(Exam.stream) == student.stream.strip().lower(),
            )
        )
    else:
        exam_query = exam_query.where(
            or_(Exam.stream.is_(None), func.lower(Exam.stream).in_(["*", "all streams"]))
        )
    exams = db.session.scalars(exam_query.order_by(Exam.created_at.desc())).all()
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
    if exam.status != "published" or not _student_can_access_exam(exam, current_user()):
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
    if not _student_can_access_exam(exam, current_user()):
        abort(404)
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
        if attempt.submitted_at is None and attempt.deadline_at and utcnow() >= attempt.deadline_at:
            _submit_attempt(attempt, reason="time_expired")
            flash("Your examination time has ended and the saved answers were submitted.")
            return redirect(url_for("student_result", attempt_id=attempt.id))
        if attempt.submitted_at is not None:
            flash("You have already submitted this examination.", "error")
            return redirect(url_for("student_result", attempt_id=attempt.id))
        return redirect(url_for("take_exam", attempt_id=attempt.id))

    attempt = Attempt(
        exam=exam,
        student=current_user(),
        started_at=None,
        deadline_at=None,
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


@app.post("/api/attempts/<int:attempt_id>/begin")
@roles_required("student")
def begin_attempt(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return jsonify(status="submitted", redirect=url_for("student_result", attempt_id=attempt.id)), 409
    if attempt.deadline_at is not None and utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt, reason="time_expired")
        return jsonify(status="submitted", redirect=url_for("student_result", attempt_id=attempt.id))
    if attempt.started_at is None:
        now = utcnow()
        attempt.started_at = now
        attempt.deadline_at = now + timedelta(minutes=attempt.exam.duration_minutes)
        db.session.commit()
    return jsonify(
        status="started",
        deadline=int(attempt.deadline_at.replace(tzinfo=timezone.utc).timestamp()),
    )


@app.get("/student/attempts/<int:attempt_id>")
@roles_required("student")
def take_exam(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return redirect(url_for("student_result", attempt_id=attempt.id))
    if attempt.deadline_at is not None and utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt, reason="time_expired")
        flash("Time expired. Your saved answers have been submitted.")
        return redirect(url_for("student_result", attempt_id=attempt.id))
    saved = {answer.question_id: answer.selected_option_id for answer in attempt.answers}
    return render_template("take_exam.html", attempt=attempt, saved=saved)


@app.post("/api/attempts/<int:attempt_id>/proctoring-violations")
@roles_required("student")
def record_proctoring_violation(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return jsonify(
            status="submitted",
            redirect=url_for("student_result", attempt_id=attempt.id),
        )
    if attempt.started_at is None or attempt.deadline_at is None:
        return jsonify(error="Complete the camera and fullscreen checks before starting."), 409
    if utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt, reason="time_expired")
        return jsonify(status="submitted", redirect=url_for("student_result", attempt_id=attempt.id))

    payload = request.get_json(silent=False)
    if not isinstance(payload, dict) or payload.get("event_type") not in PROCTORING_EVENT_TYPES:
        abort(400, description="Provide a valid proctoring event type.")

    result = db.session.execute(
        update(Attempt)
        .where(Attempt.id == attempt.id, Attempt.submitted_at.is_(None))
        .values(proctoring_violations=Attempt.proctoring_violations + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.session.rollback()
        return jsonify(error="This examination has already been submitted."), 409
    db.session.commit()
    db.session.refresh(attempt)

    if attempt.proctoring_violations >= PROCTORING_VIOLATION_LIMIT:
        _submit_attempt(attempt, reason="proctoring")
        return jsonify(
            status="submitted",
            violations=attempt.proctoring_violations,
            redirect=url_for("student_result", attempt_id=attempt.id),
        )
    return jsonify(
        status="recorded",
        violations=attempt.proctoring_violations,
        remaining=PROCTORING_VIOLATION_LIMIT - attempt.proctoring_violations,
    )


@app.post("/api/attempts/<int:attempt_id>/answers")
@roles_required("student")
def autosave_answer(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is not None:
        return jsonify(error="This examination has already been submitted."), 409
    if attempt.started_at is None or attempt.deadline_at is None:
        return jsonify(error="Complete the camera and fullscreen checks before answering."), 409
    if utcnow() >= attempt.deadline_at:
        _submit_attempt(attempt, reason="time_expired")
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
    if attempt.started_at is None:
        flash("Complete the camera and fullscreen checks before submitting.", "error")
        return redirect(url_for("take_exam", attempt_id=attempt.id))
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
        "default-src 'self'; style-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'wasm-unsafe-eval'; "
        "connect-src 'self' https://cdn.jsdelivr.net https://storage.googleapis.com; "
        "worker-src 'self' blob:; img-src 'self' data: blob:; media-src 'self' blob:; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    )
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


if __name__ == "__main__":
    app.run()
