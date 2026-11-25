import csv
import hashlib
import io
import json
import os
import random
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
    make_response,
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

from models import (
    ActivityLog,
    Answer,
    Attempt,
    CatalogEntry,
    Exam,
    Notification,
    Option,
    Question,
    QuestionBankItem,
    SystemSetting,
    User,
    db,
)


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
        and (
            not student.stream
            or stream_target != student.stream.strip().casefold()
        )
    ):
        return False
    return True


def _student_exam_query(student):
    query = select(Exam).where(Exam.status == "published", Exam.questions.any())
    if student.department:
        query = query.where(
            or_(
                Exam.department.is_(None),
                func.lower(Exam.department).in_(["*", "all departments"]),
                func.lower(Exam.department) == student.department.strip().lower(),
            )
        )
    else:
        query = query.where(
            or_(
                Exam.department.is_(None),
                func.lower(Exam.department).in_(["*", "all departments"]),
            )
        )
    if student.stream:
        query = query.where(
            or_(
                Exam.stream.is_(None),
                func.lower(Exam.stream).in_(["*", "all streams"]),
                func.lower(Exam.stream) == student.stream.strip().lower(),
            )
        )
    else:
        query = query.where(
            or_(
                Exam.stream.is_(None),
                func.lower(Exam.stream).in_(["*", "all streams"]),
            )
        )
    return query


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


def _duplicate_exam_exists(
    *,
    created_by,
    title,
    subject,
    department,
    stream,
    start_at,
    end_at,
    exclude_id=None,
):
    conditions = [
        Exam.created_by == created_by,
        func.lower(Exam.title) == title.strip().lower(),
        func.lower(Exam.subject) == subject.strip().lower(),
        func.lower(func.coalesce(Exam.department, "")) == (department or "").strip().lower(),
        func.lower(func.coalesce(Exam.stream, "")) == (stream or "").strip().lower(),
        Exam.start_at.is_(None) if start_at is None else Exam.start_at == start_at,
        Exam.end_at.is_(None) if end_at is None else Exam.end_at == end_at,
    ]
    if exclude_id is not None:
        conditions.append(Exam.id != exclude_id)
    return db.session.scalar(select(Exam.id).where(*conditions).limit(1)) is not None


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


def _save_answer(
    attempt,
    question_id,
    selected_option_id=None,
    response_text=None,
    is_marked_for_review=None,
):
    question = db.session.get(Question, question_id)
    if question is None or question.exam_id != attempt.exam_id:
        abort(400, description="That question does not belong to this examination.")

    answer = db.session.scalar(
        select(Answer).where(
            Answer.attempt_id == attempt.id,
            Answer.question_id == question.id,
        )
    )
    if answer is None:
        answer = Answer(attempt=attempt, question=question)
        db.session.add(answer)
    if question.question_type == "subjective":
        if selected_option_id is not None:
            abort(400, description="Subjective responses cannot select an answer choice.")
        response_text = (response_text or "").strip()
        if len(response_text) > 10000:
            abort(400, description="A written response is too long.")
        answer.response_text = response_text or None
        answer.selected_option = None
        answer.is_correct = False
    else:
        if response_text and response_text.strip():
            abort(400, description="Multiple-choice responses must select an answer choice.")
        if selected_option_id is None:
            answer.selected_option = None
        else:
            option = db.session.get(Option, selected_option_id)
            if option is None or option.question_id != question.id:
                abort(400, description="That answer choice is not valid for this question.")
            answer.selected_option = option
        answer.response_text = None
        answer.is_correct = False
    if is_marked_for_review is not None:
        if type(is_marked_for_review) is not bool:
            abort(400, description="Review status must be true or false.")
        answer.is_marked_for_review = is_marked_for_review


def _recalculate_attempt_score(attempt):
    total = Decimal("0")
    for answer in attempt.answers:
        if answer.question.question_type == "subjective":
            total += Decimal(answer.manual_score or 0)
            continue
        correct = (
            answer.selected_option is not None
            and answer.selected_option.question_id == answer.question_id
            and answer.selected_option.is_correct
        )
        answer.is_correct = bool(correct)
        if correct:
            total += Decimal(answer.question.marks)
        elif answer.selected_option is not None:
            total -= Decimal(attempt.exam.negative_marks_per_wrong or 0)
    attempt.score = max(
        Decimal("0"),
        min(total, Decimal(attempt.exam.total_marks)),
    )
    return attempt.score


def _pending_evaluation_count(attempt):
    return sum(
        answer.question.question_type == "subjective"
        and bool(answer.response_text and answer.response_text.strip())
        and answer.manual_score is None
        for answer in attempt.answers
    )


def _record_activity(action, target_type, target_id, summary):
    actor = current_user()
    db.session.add(
        ActivityLog(
            actor_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary[:500],
        )
    )


def _system_setting(key, default):
    setting = db.session.get(SystemSetting, key)
    return setting.value if setting else default


def _catalog_values(kind):
    return db.session.scalars(
        select(CatalogEntry.name)
        .where(CatalogEntry.kind == kind, CatalogEntry.active.is_(True))
        .order_by(CatalogEntry.name)
    ).all()


def _notify_exam_published(exam):
    students = db.session.scalars(
        select(User).where(User.role == "student", User.status == "active")
    ).all()
    for student in students:
        if _student_can_access_exam(exam, student):
            db.session.add(
                Notification(
                    user_id=student.id,
                    title="New examination available",
                    body=f"{exam.title} is now published.",
                    link=url_for("exam_instructions", exam_id=exam.id),
                )
            )


def _record_result_notification(attempt):
    teacher = attempt.exam.creator
    if teacher is not None:
        db.session.add(
            Notification(
                user_id=teacher.id,
                title="Examination submitted",
                body=f"A student submitted {attempt.exam.title}.",
                link=url_for("teacher_results"),
            )
        )


def _attempt_seed(attempt, purpose):
    material = f"{app.config['SECRET_KEY']}:{attempt.id}:{purpose}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _ordered_attempt_questions(attempt):
    questions = list(attempt.exam.questions)
    if attempt.exam.randomize_questions:
        random.Random(_attempt_seed(attempt, "questions")).shuffle(questions)
    return questions


def _ordered_attempt_options(attempt, question):
    options = list(question.options)
    if attempt.exam.randomize_options:
        random.Random(_attempt_seed(attempt, f"options:{question.id}")).shuffle(options)
    return options


def _submit_attempt(attempt, final_answers=None, reason="manual"):
    if attempt.submitted_at is not None:
        return False
    now = utcnow()
    if reason == "manual" and attempt.deadline_at is not None and now >= attempt.deadline_at:
        reason = "time_expired"
    if attempt.deadline_at is not None and now < attempt.deadline_at and final_answers is not None:
        for raw_question_id, raw_value in final_answers.items():
            try:
                question_id = int(raw_question_id)
            except (TypeError, ValueError):
                abort(400, description="An answer contains an invalid question.")
            question = db.session.get(Question, question_id)
            if question is None or question.exam_id != attempt.exam_id:
                abort(400, description="That question does not belong to this examination.")
            if question.question_type == "subjective":
                _save_answer(attempt, question_id, response_text=raw_value)
            else:
                try:
                    option_id = int(raw_value) if raw_value else None
                except (TypeError, ValueError):
                    abort(400, description="An answer contains an invalid option.")
                _save_answer(attempt, question_id, selected_option_id=option_id)

    answers_by_question = {answer.question_id: answer for answer in attempt.answers}
    for question in attempt.exam.questions:
        if question.id not in answers_by_question:
            answer = Answer(attempt=attempt, question=question)
            db.session.add(answer)
            answers_by_question[question.id] = answer

    _recalculate_attempt_score(attempt)
    attempt.submitted_at = now
    attempt.submission_reason = reason
    _record_result_notification(attempt)
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
    if _system_setting("student_registration_enabled", "true").lower() != "true":
        abort(403, description="Student registration is currently closed.")
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
    return render_template(
        "auth_form.html",
        mode="register",
        departments=_catalog_values("department"),
        streams=_catalog_values("stream"),
    )


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


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 120:
            flash("Enter a name between 1 and 120 characters.", "error")
        else:
            if user.role == "student":
                roll_number = request.form.get("roll_number", "").strip()
                if not roll_number or len(roll_number) > 50:
                    flash("Enter a roll number between 1 and 50 characters.", "error")
                    return render_template(
                        "profile.html",
                        departments=_catalog_values("department"),
                        streams=_catalog_values("stream"),
                    )
                user.roll_number = roll_number
            user.name = name
            _record_activity("profile_updated", "user", user.id, "Updated own profile.")
            db.session.commit()
            flash("Your profile was updated.")
            return redirect(url_for("profile"))
    return render_template(
        "profile.html",
        departments=_catalog_values("department"),
        streams=_catalog_values("stream"),
    )


@app.post("/profile/password")
@login_required
def change_password():
    user = current_user()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if user.password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        current_matches = bcrypt.checkpw(
            current_password.encode()[:72], user.password_hash.encode()
        )
    else:
        current_matches = check_password_hash(user.password_hash, current_password)
    if not current_matches:
        flash("The current password is incorrect.", "error")
    elif len(new_password) < 8:
        flash("Use a new password with at least 8 characters.", "error")
    elif new_password != confirm_password:
        flash("The new password entries do not match.", "error")
    else:
        user.password_hash = generate_password_hash(new_password)
        _record_activity("password_changed", "user", user.id, "Changed own password.")
        db.session.commit()
        flash("Your password was changed.")
        return redirect(url_for("profile"))
    return redirect(url_for("profile"))


@app.get("/notifications")
@login_required
def notifications():
    items = db.session.scalars(
        select(Notification)
        .where(Notification.user_id == current_user().id)
        .order_by(Notification.created_at.desc())
        .limit(100)
    ).all()
    unread_count = db.session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user().id,
            Notification.read_at.is_(None),
        )
    ) or 0
    return render_template(
        "notifications.html", notifications=items, unread_count=unread_count
    )


@app.post("/notifications/read-all")
@login_required
def mark_notifications_read():
    db.session.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user().id,
            Notification.read_at.is_(None),
        )
        .values(read_at=utcnow())
    )
    db.session.commit()
    flash("Notifications marked as read.")
    return redirect(url_for("notifications"))


@app.post("/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(notification_id):
    notification = db.session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user().id,
        )
    )
    if notification is None:
        abort(404)
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.session.commit()
    if notification.link and notification.link.startswith("/") and not notification.link.startswith("//"):
        return redirect(notification.link)
    return redirect(url_for("notifications"))


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
    now = utcnow()
    counts = {
        "students": db.session.scalar(
            select(func.count(User.id)).where(User.role == "student")
        ) or 0,
        "teachers": db.session.scalar(
            select(func.count(User.id)).where(User.role == "teacher")
        ) or 0,
        "admins": db.session.scalar(
            select(func.count(User.id)).where(User.role == "admin")
        ) or 0,
        "active_users": db.session.scalar(
            select(func.count(User.id)).where(User.status == "active")
        ) or 0,
        "inactive_users": db.session.scalar(
            select(func.count(User.id)).where(User.status == "inactive")
        ) or 0,
        "exams": db.session.scalar(select(func.count(Exam.id))) or 0,
        "questions": db.session.scalar(select(func.count(Question.id))) or 0,
        "attempts": db.session.scalar(select(func.count(Attempt.id))) or 0,
        "active_exams": db.session.scalar(
            select(func.count(Exam.id)).where(
                Exam.status == "published",
                Exam.questions.any(),
                or_(Exam.start_at.is_(None), Exam.start_at <= now),
                or_(Exam.end_at.is_(None), Exam.end_at >= now),
            )
        ) or 0,
    }
    chart_data = {
        "users": [
            {"label": "Students", "value": counts["students"]},
            {"label": "Teachers", "value": counts["teachers"]},
            {"label": "Admins", "value": counts["admins"]},
        ],
        "exams": [
            {
                "label": status.title(),
                "value": db.session.scalar(
                    select(func.count(Exam.id)).where(Exam.status == status)
                ) or 0,
            }
            for status in ("published", "draft", "closed")
        ],
    }
    recent_exams = db.session.scalars(select(Exam).order_by(Exam.created_at.desc()).limit(6)).all()
    return render_template(
        "admin_dashboard.html",
        counts=counts,
        recent_exams=recent_exams,
        chart_data=chart_data,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@roles_required("admin")
def admin_users():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        roll_number = request.form.get("roll_number", "").strip()
        department = request.form.get("department", "").strip()
        stream = request.form.get("stream", "").strip()
        if (
            not name
            or "@" not in email
            or len(password) < 8
            or role not in {"admin", "teacher", "student"}
            or (
                role == "student"
                and (not roll_number or not department or not stream)
            )
        ):
            flash("Enter valid account details and complete the required student fields.", "error")
        else:
            account = User(
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
                roll_number=roll_number or None,
                department=department or None,
                stream=stream or None,
            )
            db.session.add(account)
            try:
                db.session.flush()
                _record_activity(
                    "account_created",
                    "user",
                    account.id,
                    f"Created a {role} account.",
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email address is already registered.", "error")
            else:
                flash(f"{role.title()} account created.")
                return redirect(url_for("admin_users"))
    role_filter = request.args.get("role", "")
    status_filter = request.args.get("status", "")
    query = select(User).order_by(User.created_at.desc())
    if role_filter in {"admin", "teacher", "student"}:
        query = query.where(User.role == role_filter)
    else:
        role_filter = ""
    if status_filter in {"active", "inactive"}:
        query = query.where(User.status == status_filter)
    else:
        status_filter = ""
    users = db.session.scalars(query).all()
    return render_template(
        "admin_users.html",
        users=users,
        selected_role=role_filter,
        selected_status=status_filter,
        departments=_catalog_values("department"),
        streams=_catalog_values("stream"),
    )


@app.post("/admin/users/<int:user_id>/status")
@roles_required("admin")
def update_user_status(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user().id:
        flash("You cannot deactivate your own administrator account.", "error")
    else:
        user.status = "inactive" if user.status == "active" else "active"
        _record_activity(
            "account_status_changed",
            "user",
            user.id,
            f"Changed {user.role} account status to {user.status}.",
        )
        db.session.commit()
        flash(f"{user.name}'s account is now {user.status}.")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/password")
@roles_required("admin")
def reset_user_password(user_id):
    user = db.get_or_404(User, user_id)
    password = request.form.get("password", "")
    if user.id == current_user().id:
        flash("Use the profile page to change your own password.", "error")
    elif len(password) < 8:
        flash("The new password must contain at least 8 characters.", "error")
    else:
        user.password_hash = generate_password_hash(password)
        _record_activity("password_reset", "user", user.id, "An administrator reset an account password.")
        db.session.commit()
        flash("The password was reset. Share the temporary password through a secure channel.")
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
    _record_activity("account_deleted", "user", user_id, "An administrator deleted an account.")
    db.session.commit()
    flash(f"{user.name}'s account was deleted.")
    return redirect(url_for("admin_users"))


@app.route("/admin/catalog", methods=["GET", "POST"])
@roles_required("admin")
def admin_catalog():
    if request.method == "POST":
        kind = request.form.get("kind", "")
        name = request.form.get("name", "").strip()
        if kind not in {"subject", "department", "stream"} or not name or len(name) > 120:
            flash("Choose a valid catalog type and enter a name up to 120 characters.", "error")
        else:
            duplicate = db.session.scalar(
                select(CatalogEntry.id).where(
                    CatalogEntry.kind == kind,
                    func.lower(CatalogEntry.name) == name.casefold(),
                )
            )
            if duplicate:
                flash("That catalog entry already exists.", "error")
            else:
                entry = CatalogEntry(
                    kind=kind,
                    name=name,
                    created_by_id=current_user().id,
                )
                db.session.add(entry)
                try:
                    db.session.flush()
                    _record_activity(
                        "catalog_entry_created",
                        "catalog",
                        entry.id,
                        f"Added a {kind} catalog entry.",
                    )
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("That catalog entry already exists.", "error")
                else:
                    flash(f"{kind.title()} added.")
                    return redirect(url_for("admin_catalog"))
    entries = db.session.scalars(
        select(CatalogEntry).order_by(CatalogEntry.kind, CatalogEntry.name)
    ).all()
    return render_template("admin_catalog.html", entries=entries)


@app.post("/admin/catalog/<int:entry_id>/toggle")
@roles_required("admin")
def toggle_catalog_entry(entry_id):
    entry = db.get_or_404(CatalogEntry, entry_id)
    entry.active = not entry.active
    _record_activity(
        "catalog_entry_status_changed",
        "catalog",
        entry.id,
        f"Changed a {entry.kind} catalog entry to {'active' if entry.active else 'inactive'}.",
    )
    db.session.commit()
    flash("Catalog entry status updated.")
    return redirect(url_for("admin_catalog"))


@app.route("/admin/settings", methods=["GET", "POST"])
@roles_required("admin")
def admin_settings():
    defaults = {
        "student_registration_enabled": "true",
        "default_exam_duration": "60",
        "default_negative_marks_per_wrong": "0",
    }
    if request.method == "POST":
        registration_enabled = request.form.get("student_registration_enabled") == "on"
        try:
            duration = int(request.form.get("default_exam_duration", ""))
            negative_marks = Decimal(
                request.form.get("default_negative_marks_per_wrong", "")
            )
        except (ValueError, InvalidOperation):
            flash("Enter a valid exam duration and negative-mark value.", "error")
        else:
            if (
                not 1 <= duration <= 10080
                or not negative_marks.is_finite()
                or negative_marks < 0
                or negative_marks > Decimal("9999.99")
            ):
                flash("Review the settings and enter values within the allowed ranges.", "error")
            else:
                updates = {
                    "student_registration_enabled": str(registration_enabled).lower(),
                    "default_exam_duration": str(duration),
                    "default_negative_marks_per_wrong": str(negative_marks),
                }
                for key, value in updates.items():
                    setting = db.session.get(SystemSetting, key)
                    if setting is None:
                        setting = SystemSetting(key=key, value=value)
                        db.session.add(setting)
                    else:
                        setting.value = value
                    setting.updated_by_id = current_user().id
                _record_activity(
                    "system_settings_updated",
                    "system",
                    None,
                    "Updated registration and exam defaults.",
                )
                db.session.commit()
                flash("System settings saved.")
                return redirect(url_for("admin_settings"))
    settings = {key: _system_setting(key, value) for key, value in defaults.items()}
    return render_template("admin_settings.html", settings=settings)


@app.get("/admin/activity")
@roles_required("admin")
def admin_activity():
    entries = db.session.scalars(
        select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(200)
    ).all()
    return render_template("admin_activity.html", entries=entries)


@app.get("/admin/monitoring")
@roles_required("admin")
def admin_monitoring():
    attempts = db.session.scalars(
        select(Attempt).join(Exam).order_by(Attempt.id.desc()).limit(200)
    ).all()
    return render_template("admin_monitoring.html", attempts=attempts)


@app.get("/teacher")
@roles_required("teacher", "admin")
def teacher_dashboard():
    user = current_user()
    exams_query = select(Exam).order_by(Exam.created_at.desc())
    if user.role == "teacher":
        exams_query = exams_query.where(Exam.created_by == user.id)
    exams = db.session.scalars(exams_query).all()
    exam_ids = [exam.id for exam in exams]
    now = utcnow()
    if exam_ids:
        pending_evaluations = db.session.scalar(
            select(func.count(Answer.id))
            .join(Attempt, Answer.attempt_id == Attempt.id)
            .join(Question, Answer.question_id == Question.id)
            .where(
                Attempt.exam_id.in_(exam_ids),
                Attempt.submitted_at.is_not(None),
                Question.question_type == "subjective",
                Answer.response_text.is_not(None),
                Answer.manual_score.is_(None),
            )
        ) or 0
    else:
        pending_evaluations = 0
    active_exams = sum(
        exam.status == "published"
        and bool(exam.questions)
        and (exam.start_at is None or exam.start_at <= now)
        and (exam.end_at is None or exam.end_at >= now)
        for exam in exams
    )
    metrics = {
        "exams": len(exams),
        "students": db.session.scalar(
            select(func.count(User.id)).where(User.role == "student")
        ) or 0,
        "active_exams": active_exams,
        "pending_evaluations": pending_evaluations,
        "attempts": db.session.scalar(
            select(func.count(Attempt.id)).where(Attempt.exam_id.in_(exam_ids))
        ) or 0 if exam_ids else 0,
    }
    return render_template(
        "teacher_dashboard.html",
        exams=exams,
        metrics=metrics,
    )


@app.route("/teacher/exams/new", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def create_exam():
    if request.method == "POST":
        try:
            duration = int(request.form.get("duration_minutes", ""))
            total_marks = Decimal(request.form.get("total_marks", ""))
            target_question_count = int(request.form.get("target_question_count", ""))
            negative_marks = Decimal(request.form.get("negative_marks_per_wrong", "0") or "0")
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
            status = request.form.get("status", "draft")
            if (
                not title
                or len(title) > 180
                or not subject
                or len(subject) > 120
                or not department
                or not stream
                or not topics
                or status not in {"draft", "published", "closed"}
                or len(department) > 120
                or len(stream) > 120
                or len(topics) > 2000
                or not 1 <= target_question_count <= 50
                or not 1 <= duration <= 10080
                or not total_marks.is_finite()
                or not Decimal("0") < total_marks <= Decimal("999999.99")
                or not negative_marks.is_finite()
                or negative_marks < 0
                or negative_marks > total_marks
                or (start_at and end_at and end_at <= start_at)
            ):
                flash("Enter valid exam details. The end time must be after the start time.", "error")
            elif _duplicate_exam_exists(
                created_by=current_user().id,
                title=title,
                subject=subject,
                department=department,
                stream=stream,
                start_at=start_at,
                end_at=end_at,
            ):
                flash("An examination with the same title, subject, audience, and schedule already exists.", "error")
            else:
                if status == "published":
                    status = "draft"
                    flash("The examination was saved as a draft. Add questions before publishing it.")
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
                    negative_marks_per_wrong=negative_marks,
                    randomize_questions=request.form.get("randomize_questions") == "on",
                    randomize_options=request.form.get("randomize_options") == "on",
                    start_at=start_at,
                    end_at=end_at,
                    created_by=current_user().id,
                    status=status,
                )
                db.session.add(exam)
                db.session.flush()
                _record_activity("exam_created", "exam", exam.id, f"Created {exam.title}.")
                db.session.commit()
                if status != "draft":
                    flash("Examination created.")
                elif not request.form.get("status") == "published":
                    flash("Examination created. Add questions, then publish it when ready.")
                return redirect(url_for("manage_questions", exam_id=exam.id))
    return render_template(
        "exam_form.html",
        exam=None,
        catalog={
            "subjects": _catalog_values("subject"),
            "departments": _catalog_values("department"),
            "streams": _catalog_values("stream"),
        },
        default_duration=_system_setting("default_exam_duration", "60"),
        default_negative_marks=_system_setting("default_negative_marks_per_wrong", "0"),
    )


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
            negative_marks = Decimal(request.form.get("negative_marks_per_wrong", "0") or "0")
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
                or len(title) > 180
                or not subject
                or len(subject) > 120
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
                or not negative_marks.is_finite()
                or negative_marks < 0
                or negative_marks > total_marks
                or status not in {"draft", "published", "closed"}
                or (status == "published" and not exam.questions)
                or (start_at and end_at and end_at <= start_at)
            ):
                flash("Review the exam fields and enter a valid schedule.", "error")
            elif _duplicate_exam_exists(
                created_by=exam.created_by,
                title=title,
                subject=subject,
                department=department,
                stream=stream,
                start_at=start_at,
                end_at=end_at,
                exclude_id=exam.id,
            ):
                flash("An examination with the same title, subject, audience, and schedule already exists.", "error")
            else:
                old_status = exam.status
                exam.title = title
                exam.subject = subject
                exam.department = department
                exam.stream = stream
                exam.topics = topics
                exam.target_question_count = target_question_count
                exam.description = request.form.get("description", "").strip()
                exam.duration_minutes = duration
                exam.total_marks = total_marks
                exam.negative_marks_per_wrong = negative_marks
                exam.randomize_questions = request.form.get("randomize_questions") == "on"
                exam.randomize_options = request.form.get("randomize_options") == "on"
                exam.start_at = start_at
                exam.end_at = end_at
                exam.status = status
                if old_status != "published" and status == "published":
                    _notify_exam_published(exam)
                _record_activity("exam_updated", "exam", exam.id, f"Updated {exam.title}.")
                db.session.commit()
                flash("Examination updated.")
                return redirect(url_for("teacher_dashboard"))
    return render_template(
        "exam_form.html",
        exam=exam,
        catalog={
            "subjects": sorted({value for value in _catalog_values("subject") + [exam.subject] if value}),
            "departments": sorted({value for value in _catalog_values("department") + [exam.department] if value}),
            "streams": sorted({value for value in _catalog_values("stream") + [exam.stream] if value}),
        },
        default_duration=_system_setting("default_exam_duration", "60"),
        default_negative_marks=_system_setting("default_negative_marks_per_wrong", "0"),
    )


@app.post("/teacher/exams/<int:exam_id>/status")
@roles_required("teacher", "admin")
def update_exam_status(exam_id):
    exam = _exam_access(exam_id)
    status = request.form.get("status", "")
    if status not in {"draft", "published", "closed"}:
        abort(400, description="Choose a valid examination status.")
    if status == "published" and not exam.questions:
        flash("Add at least one question before publishing this examination.", "error")
    elif status != exam.status:
        previous_status = exam.status
        exam.status = status
        if status == "published" and previous_status != "published":
            _notify_exam_published(exam)
        _record_activity(
            "exam_status_changed",
            "exam",
            exam.id,
            f"Changed {exam.title} status to {status}.",
        )
        db.session.commit()
        flash(f"Examination is now {status}.")
    return redirect(url_for("teacher_dashboard"))


@app.post("/teacher/exams/<int:exam_id>/delete")
@roles_required("teacher", "admin")
def delete_exam(exam_id):
    exam = _exam_access(exam_id)
    if exam.attempts:
        flash("An examination with student attempts cannot be deleted.", "error")
    else:
        exam_id = exam.id
        title = exam.title
        _record_activity("exam_deleted", "exam", exam_id, f"Deleted {title}.")
        db.session.delete(exam)
        db.session.commit()
        flash("Examination deleted.")
    return redirect(url_for("teacher_dashboard"))


@app.route("/teacher/exams/<int:exam_id>/questions", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def manage_questions(exam_id):
    exam = _exam_access(exam_id)
    if request.method == "POST":
        if exam.attempts:
            flash("Questions are locked because a student has already started this examination.", "error")
            return redirect(url_for("manage_questions", exam_id=exam.id))
        question_text = request.form.get("question_text", "").strip()
        question_type = request.form.get("question_type", "mcq")
        options = [item.strip() for item in request.form.getlist("options")]
        try:
            marks = Decimal(request.form.get("marks", "1"))
            correct_index = (
                int(request.form.get("correct_index", ""))
                if question_type == "mcq"
                else None
            )
        except (ValueError, InvalidOperation):
            flash("Enter valid marks and select the correct answer.", "error")
        else:
            if (
                not question_text
                or question_type not in {"mcq", "subjective"}
                or (
                    question_type == "mcq"
                    and (
                        len(options) != 4
                        or any(not option for option in options)
                        or correct_index not in range(4)
                    )
                )
                or not marks.is_finite()
                or not Decimal("0") < marks <= Decimal("9999.99")
                or (question_type == "subjective" and len(question_text) > 10000)
            ):
                flash("Provide a valid question, positive marks, and four choices for MCQs.", "error")
            else:
                question = Question(
                    exam=exam,
                    question_text=question_text,
                    question_type=question_type,
                    marks=marks,
                )
                if question_type == "mcq":
                    question.options = [
                        Option(option_text=text, is_correct=index == correct_index)
                        for index, text in enumerate(options)
                    ]
                db.session.add(question)
                db.session.flush()
                _record_activity(
                    "question_created",
                    "question",
                    question.id,
                    f"Added a {question_type} question to {exam.title}.",
                )
                db.session.commit()
                flash("Question added.")
                return redirect(url_for("manage_questions", exam_id=exam.id))
    return render_template(
        "questions.html",
        exam=exam,
        questions_locked=bool(exam.attempts),
        ai_available=bool(os.environ.get("GEMINI_API_KEY")),
    )


def _read_question_import(file_storage):
    extension = Path(file_storage.filename or "").suffix.lower()
    if extension == ".csv":
        try:
            text = file_storage.read().decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("CSV files must use UTF-8 encoding.") from error
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    elif extension == ".xlsx":
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
        from zipfile import BadZipFile

        try:
            workbook = load_workbook(
                io.BytesIO(file_storage.read()), read_only=True, data_only=True
            )
        except (BadZipFile, EOFError, InvalidFileException, OSError) as error:
            raise ValueError("The uploaded Excel file is invalid or unreadable.") from error
        sheet = workbook.active
        records = list(sheet.iter_rows(values_only=True))
        if not records:
            raise ValueError("The spreadsheet is empty.")
        headers = [str(value or "").strip().lower() for value in records[0]]
        rows = [
            dict(zip(headers, record))
            for record in records[1:]
            if any(value is not None for value in record)
        ]
        workbook.close()
    else:
        raise ValueError("Choose a .csv or .xlsx file.")

    if not rows:
        raise ValueError("The import file does not contain any question rows.")
    if len(rows) > 100:
        raise ValueError("Import up to 100 questions at a time.")
    required_headers = {"question_text", "marks"}
    headers = {str(key or "").strip().lower() for key in (rows[0] or {})}
    if not required_headers.issubset(headers):
        raise ValueError("Include question_text and marks columns in the import file.")

    questions = []
    for row_number, raw_row in enumerate(rows, start=2):
        row = {str(key or "").strip().lower(): value for key, value in raw_row.items()}
        text = str(row.get("question_text") or "").strip()
        question_type = str(row.get("type") or "mcq").strip().lower()
        try:
            marks = Decimal(str(row.get("marks") or ""))
        except InvalidOperation as error:
            raise ValueError(f"Row {row_number} has invalid marks.") from error
        if (
            not text
            or len(text) > 10000
            or question_type not in {"mcq", "subjective"}
            or not marks.is_finite()
            or not Decimal("0") < marks <= Decimal("9999.99")
        ):
            raise ValueError(f"Row {row_number} contains invalid question data.")

        options = []
        correct_index = None
        if question_type == "mcq":
            options = [
                str(row.get(f"option_{letter}") or "").strip()
                for letter in ("a", "b", "c", "d")
            ]
            correct_value = str(
                row.get("correct_option", row.get("correct_index", ""))
            ).strip().upper()
            correct_index = (
                ord(correct_value) - ord("A")
                if correct_value in {"A", "B", "C", "D"}
                else int(correct_value) if correct_value in {"0", "1", "2", "3"} else -1
            )
            if any(not option or len(option) > 2000 for option in options) or correct_index not in range(4):
                raise ValueError(f"Row {row_number} needs four choices and a valid correct_option (A-D).")
        questions.append(
            {
                "question_text": text,
                "question_type": question_type,
                "marks": marks,
                "options": options,
                "correct_index": correct_index,
                "subject": str(row.get("subject") or "").strip(),
            }
        )
    return questions


@app.route("/teacher/question-bank", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def question_bank():
    if request.method == "POST":
        question_text = request.form.get("question_text", "").strip()
        subject = request.form.get("subject", "").strip()
        question_type = request.form.get("question_type", "mcq")
        raw_options = [value.strip() for value in request.form.getlist("options")]
        try:
            marks = Decimal(request.form.get("marks", "1"))
            correct_index = (
                int(request.form.get("correct_index", ""))
                if question_type == "mcq"
                else None
            )
        except (ValueError, InvalidOperation):
            flash("Enter valid marks and select the correct MCQ choice.", "error")
        else:
            if (
                not question_text
                or not subject
                or len(question_text) > 10000
                or len(subject) > 120
                or question_type not in {"mcq", "subjective"}
                or not marks.is_finite()
                or not Decimal("0") < marks <= Decimal("9999.99")
                or (
                    question_type == "mcq"
                    and (
                        len(raw_options) != 4
                        or any(not option for option in raw_options)
                        or correct_index not in range(4)
                    )
                )
            ):
                flash("Review the question, subject, marks, and MCQ choices.", "error")
            else:
                item = QuestionBankItem(
                    owner_id=current_user().id,
                    subject=subject,
                    question_text=question_text,
                    question_type=question_type,
                    marks=marks,
                    options=raw_options if question_type == "mcq" else [],
                    correct_index=correct_index,
                )
                db.session.add(item)
                db.session.flush()
                _record_activity(
                    "question_bank_item_created",
                    "question_bank",
                    item.id,
                    f"Added a {question_type} item to the question bank.",
                )
                db.session.commit()
                flash("Question saved to the bank.")
                return redirect(url_for("question_bank"))

    query = select(QuestionBankItem).order_by(QuestionBankItem.created_at.desc())
    if current_user().role == "teacher":
        query = query.where(QuestionBankItem.owner_id == current_user().id)
    items = db.session.scalars(query.limit(500)).all()
    exams_query = select(Exam).where(~Exam.attempts.any()).order_by(Exam.title)
    if current_user().role == "teacher":
        exams_query = exams_query.where(Exam.created_by == current_user().id)
    exams = db.session.scalars(exams_query).all()
    return render_template(
        "question_bank.html",
        items=items,
        exams=exams,
        subjects=_catalog_values("subject"),
    )


@app.post("/teacher/question-bank/import")
@roles_required("teacher", "admin")
def import_question_bank():
    upload = request.files.get("question_file")
    if upload is None or not upload.filename:
        flash("Choose a CSV or Excel file to import.", "error")
        return redirect(url_for("question_bank"))
    try:
        imported = _read_question_import(upload)
    except (ValueError, OSError) as error:
        flash(str(error), "error")
        return redirect(url_for("question_bank"))

    items = [
        QuestionBankItem(
            owner_id=current_user().id,
            subject=item["subject"] or request.form.get("subject", "").strip(),
            question_text=item["question_text"],
            question_type=item["question_type"],
            marks=item["marks"],
            options=item["options"],
            correct_index=item["correct_index"],
        )
        for item in imported
    ]
    if any(not item.subject or len(item.subject) > 120 for item in items):
        flash("Provide a subject in the file or import form for every question.", "error")
        return redirect(url_for("question_bank"))
    db.session.add_all(items)
    db.session.flush()
    _record_activity(
        "question_bank_imported",
        "question_bank",
        None,
        f"Imported {len(items)} question bank items.",
    )
    db.session.commit()
    flash(f"Imported {len(items)} questions.")
    return redirect(url_for("question_bank"))


@app.post("/teacher/question-bank/<int:item_id>/copy")
@roles_required("teacher", "admin")
def add_bank_question_to_exam(item_id):
    item = db.get_or_404(QuestionBankItem, item_id)
    if current_user().role == "teacher" and item.owner_id != current_user().id:
        abort(403)
    exam = _exam_access(request.form.get("exam_id", type=int))
    if exam.attempts:
        flash("Questions are locked because a student has started this examination.", "error")
    else:
        question = Question(
            exam_id=exam.id,
            question_text=item.question_text,
            question_type=item.question_type,
            marks=item.marks,
        )
        if item.question_type == "mcq":
            question.options = [
                Option(option_text=text, is_correct=index == item.correct_index)
                for index, text in enumerate(item.options)
            ]
        db.session.add(question)
        exam.target_question_count = len(exam.questions) + 1
        _record_activity(
            "question_bank_item_used",
            "question",
            item.id,
            f"Copied a bank question into {exam.title}.",
        )
        db.session.commit()
        flash("Question added to the examination.")
    return redirect(url_for("manage_questions", exam_id=exam.id))


@app.post("/teacher/question-bank/<int:item_id>/delete")
@roles_required("teacher", "admin")
def delete_question_bank_item(item_id):
    item = db.get_or_404(QuestionBankItem, item_id)
    if current_user().role == "teacher" and item.owner_id != current_user().id:
        abort(403)
    db.session.delete(item)
    _record_activity(
        "question_bank_item_deleted",
        "question_bank",
        item_id,
        "Deleted a question bank item.",
    )
    db.session.commit()
    flash("Question removed from the bank.")
    return redirect(url_for("question_bank"))


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
        question_id = question.id
        _record_activity("question_deleted", "question", question_id, "Deleted an examination question.")
        db.session.delete(question)
        db.session.commit()
        flash("Question removed.")
    return redirect(url_for("manage_questions", exam_id=exam.id))


def _teacher_attempt_query():
    query = select(Attempt).join(Exam).where(Attempt.submitted_at.is_not(None))
    if current_user().role == "teacher":
        query = query.where(Exam.created_by == current_user().id)
    exam_id = request.args.get("exam_id", type=int)
    if exam_id is not None:
        _exam_access(exam_id)
        query = query.where(Attempt.exam_id == exam_id)
    return query


def _csv_safe(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _csv_download(filename, headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_safe(value) for value in row])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _pdf_download(filename, title, headers, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    def safe_text(value):
        text = str(value if value is not None else "").encode("latin-1", "replace").decode("latin-1")
        return escape(text)

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "CampusExamTableCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=9,
        spaceAfter=0,
    )
    data = [[Paragraph(safe_text(value), cell_style) for value in headers]]
    data.extend(
        [[Paragraph(safe_text(value), cell_style) for value in row] for row in rows]
    )
    table = Table(
        data,
        colWidths=[document.width / max(len(headers), 1)] * len(headers),
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#202942")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dfe3ed")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fb")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    document.build(
        [
            Paragraph(safe_text(title), styles["Title"]),
            Spacer(1, 7 * mm),
            table,
        ]
    )
    response = make_response(buffer.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.get("/teacher/results")
@roles_required("teacher", "admin")
def teacher_results():
    attempts = db.session.scalars(
        _teacher_attempt_query().order_by(Attempt.submitted_at.desc())
    ).all()
    pending = {attempt.id: _pending_evaluation_count(attempt) for attempt in attempts}
    return render_template(
        "teacher_results.html", attempts=attempts, pending_evaluations=pending
    )


@app.route("/teacher/attempts/<int:attempt_id>", methods=["GET", "POST"])
@roles_required("teacher", "admin")
def teacher_attempt_detail(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    _exam_access(attempt.exam_id)
    if attempt.submitted_at is None:
        abort(409, description="Only submitted examinations can be evaluated.")
    if request.method == "POST":
        updates = []
        try:
            for answer in attempt.answers:
                if (
                    answer.question.question_type != "subjective"
                    or not (answer.response_text and answer.response_text.strip())
                ):
                    continue
                raw_score = request.form.get(f"score_{answer.id}", "").strip()
                if not raw_score:
                    continue
                score = Decimal(raw_score)
                if (
                    not score.is_finite()
                    or score < 0
                    or score > Decimal(answer.question.marks)
                ):
                    raise ValueError("Each score must be between zero and the question’s marks.")
                feedback = request.form.get(f"feedback_{answer.id}", "").strip()
                if len(feedback) > 5000:
                    raise ValueError("Feedback must be 5,000 characters or fewer.")
                updates.append((answer, score, feedback))
        except (InvalidOperation, ValueError) as error:
            flash(str(error) or "Enter a valid score.", "error")
        else:
            if not updates:
                flash("Enter a score for at least one written response.", "error")
            else:
                for answer, score, feedback in updates:
                    answer.manual_score = score
                    answer.feedback = feedback or None
                    answer.graded_by_id = current_user().id
                    answer.graded_at = utcnow()
                _recalculate_attempt_score(attempt)
                _record_activity(
                    "subjective_answers_graded",
                    "attempt",
                    attempt.id,
                    f"Graded {len(updates)} written responses for {attempt.exam.title}.",
                )
                db.session.commit()
                flash(f"Saved {len(updates)} written-response grade(s).")
                return redirect(url_for("teacher_attempt_detail", attempt_id=attempt.id))
    return render_template(
        "teacher_attempt_detail.html",
        attempt=attempt,
        pending_count=_pending_evaluation_count(attempt),
    )


@app.get("/teacher/results.csv")
@roles_required("teacher", "admin")
def teacher_results_csv():
    attempts = db.session.scalars(
        _teacher_attempt_query().order_by(Attempt.submitted_at.desc())
    ).all()
    rows = []
    for attempt in attempts:
        total = Decimal(attempt.exam.total_marks)
        rows.append(
            [
                attempt.student.name,
                attempt.student.roll_number or attempt.student.email,
                attempt.exam.title,
                attempt.exam.subject,
                attempt.score if not _pending_evaluation_count(attempt) else "Pending",
                total,
                round(float(attempt.score / total * 100), 1) if total and not _pending_evaluation_count(attempt) else "",
                attempt.submitted_at.isoformat(),
            ]
        )
    return _csv_download(
        "campusexam-results.csv",
        ["Student", "Roll / Email", "Examination", "Subject", "Score", "Total", "Percent", "Submitted"],
        rows,
    )


@app.get("/teacher/results.pdf")
@roles_required("teacher", "admin")
def teacher_results_pdf():
    attempts = db.session.scalars(
        _teacher_attempt_query().order_by(Attempt.submitted_at.desc())
    ).all()
    rows = []
    for attempt in attempts:
        total = Decimal(attempt.exam.total_marks)
        pending = _pending_evaluation_count(attempt)
        rows.append(
            [
                attempt.student.name,
                attempt.exam.title,
                attempt.exam.subject,
                "Pending" if pending else f"{attempt.score} / {total}",
                "Pending" if pending else f"{float(attempt.score / total * 100):.1f}%",
                attempt.submitted_at.strftime("%Y-%m-%d %H:%M UTC"),
            ]
        )
    return _pdf_download(
        "campusexam-results.pdf",
        "CampusExam student results",
        ["Student", "Examination", "Subject", "Score", "Percent", "Submitted"],
        rows,
    )


@app.get("/student")
@roles_required("student")
def student_dashboard():
    student = current_user()
    exams = db.session.scalars(
        _student_exam_query(student).order_by(Exam.start_at, Exam.created_at.desc())
    ).all()
    attempts = {
        attempt.exam_id: attempt
        for attempt in db.session.scalars(
            select(Attempt).where(Attempt.student_id == current_user().id)
        ).all()
    }
    now = utcnow()
    completed_attempts = sorted(
        (attempt for attempt in attempts.values() if attempt.submitted_at is not None),
        key=lambda attempt: attempt.submitted_at,
        reverse=True,
    )
    pending_evaluations = {
        attempt.id: _pending_evaluation_count(attempt)
        for attempt in completed_attempts
    }
    upcoming_exams = [
        exam
        for exam in exams
        if exam.start_at is not None
        and exam.start_at > now
        and exam.id not in attempts
    ]
    available_exams = [
        exam
        for exam in exams
        if _exam_open(exam, now)
        and (
            exam.id not in attempts
            or attempts[exam.id].submitted_at is None
        )
    ]
    percentages = [
        float(attempt.score / attempt.exam.total_marks * 100)
        for attempt in completed_attempts
        if attempt.exam.total_marks and not _pending_evaluation_count(attempt)
    ]
    subject_scores = {}
    for attempt in completed_attempts:
        if not attempt.exam.total_marks or _pending_evaluation_count(attempt):
            continue
        values = subject_scores.setdefault(attempt.exam.subject, [])
        values.append(float(attempt.score / attempt.exam.total_marks * 100))
    subject_performance = [
        {"label": subject, "value": round(sum(scores) / len(scores), 1)}
        for subject, scores in sorted(subject_scores.items())
    ]
    overall_performance = [
        {
            "label": attempt.submitted_at.strftime("%b %d"),
            "value": round(float(attempt.score / attempt.exam.total_marks * 100), 1),
        }
        for attempt in reversed(completed_attempts[-10:])
        if attempt.exam.total_marks and not _pending_evaluation_count(attempt)
    ]
    notifications_list = db.session.scalars(
        select(Notification)
        .where(Notification.user_id == student.id)
        .order_by(Notification.created_at.desc())
        .limit(5)
    ).all()
    unread_count = db.session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == student.id,
            Notification.read_at.is_(None),
        )
    ) or 0
    stats = {
        "available": len(available_exams),
        "completed": len(completed_attempts),
        "upcoming": len(upcoming_exams),
        "average_score": round(sum(percentages) / len(percentages), 1) if percentages else None,
    }
    return render_template(
        "student_dashboard.html",
        exams=exams,
        attempts=attempts,
        now=now,
        stats=stats,
        available_exams=available_exams,
        upcoming_exams=upcoming_exams,
        completed_attempts=completed_attempts[:5],
        pending_evaluations=pending_evaluations,
        subject_performance=subject_performance,
        overall_performance=overall_performance,
        notifications=notifications_list,
        unread_count=unread_count,
    )


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
    saved = {
        answer.question_id: {
            "option_id": answer.selected_option_id,
            "response_text": answer.response_text or "",
            "review": answer.is_marked_for_review,
        }
        for answer in attempt.answers
    }
    questions = _ordered_attempt_questions(attempt)
    options_by_question = {
        question.id: _ordered_attempt_options(attempt, question)
        for question in questions
    }
    return render_template(
        "take_exam.html",
        attempt=attempt,
        saved=saved,
        questions=questions,
        options_by_question=options_by_question,
    )


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
    if not isinstance(payload, dict):
        abort(400, description="Provide a valid answer payload.")
    try:
        question_id = int(payload["question_id"])
        raw_option_id = payload.get("option_id")
        option_id = int(raw_option_id) if raw_option_id is not None else None
    except (TypeError, ValueError, KeyError):
        abort(400, description="Provide a valid question_id and option_id.")
    response_text = payload.get("response_text")
    if response_text is not None and not isinstance(response_text, str):
        abort(400, description="A written response must be text.")
    _save_answer(
        attempt,
        question_id,
        option_id,
        response_text=response_text,
        is_marked_for_review=payload.get("is_marked_for_review"),
    )
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
    pending = {attempt.id: _pending_evaluation_count(attempt) for attempt in attempts}
    return render_template(
        "student_results.html",
        attempts=attempts,
        selected=None,
        pending_evaluations=pending,
        selected_pending=0,
    )


@app.get("/student/results/<int:attempt_id>")
@roles_required("student")
def student_result(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is None:
        return redirect(url_for("take_exam", attempt_id=attempt.id))
    return render_template(
        "student_results.html",
        attempts=[attempt],
        selected=attempt,
        pending_evaluations={attempt.id: _pending_evaluation_count(attempt)},
        selected_pending=_pending_evaluation_count(attempt),
    )


@app.get("/student/results/<int:attempt_id>/pdf")
@roles_required("student")
def student_result_pdf(attempt_id):
    attempt = _active_attempt(attempt_id)
    if attempt.submitted_at is None:
        abort(404)
    pending = _pending_evaluation_count(attempt)
    total = Decimal(attempt.exam.total_marks)
    rows = [
        [
            attempt.student.name,
            attempt.student.roll_number or attempt.student.email,
            attempt.exam.title,
            attempt.exam.subject,
            "Pending manual grading" if pending else f"{attempt.score} / {total}",
            "Pending" if pending else f"{float(attempt.score / total * 100):.1f}%",
            attempt.submitted_at.strftime("%Y-%m-%d %H:%M UTC"),
        ]
    ]
    return _pdf_download(
        f"campusexam-result-{attempt.id}.pdf",
        "CampusExam examination result",
        ["Student", "Roll / Email", "Examination", "Subject", "Score", "Percent", "Submitted"],
        rows,
    )


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
