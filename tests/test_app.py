import os
import json
import io
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import bcrypt
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-only-secret-key"

import app as application
from app import app, db
from models import Attempt, Exam, Option, Question, User


def test_local_database_configuration_defaults_to_instance_sqlite(tmp_path):
    settings = application._database_configuration(tmp_path, {})
    url = make_url(settings["database_url"])

    assert url.get_backend_name() == "sqlite"
    assert url.database == str(tmp_path / "college_exam_system.sqlite3")
    assert not settings["production"]


def test_production_database_configuration_requires_persistent_postgresql(tmp_path):
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        application._database_configuration(tmp_path, {"APP_ENV": "production"})

    with pytest.raises(RuntimeError, match="requires a persistent PostgreSQL"):
        application._database_configuration(
            tmp_path,
            {"APP_ENV": "production", "DATABASE_URL": "sqlite:///local.sqlite3"},
        )

    settings = application._database_configuration(
        tmp_path,
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgres://user:pass@example.test/exams",
            "TRUSTED_HOSTS": "exams.example.edu, .vercel.app",
        },
    )
    url = make_url(settings["database_url"])
    assert url.drivername == "postgresql+psycopg"
    assert settings["production"]
    assert settings["engine_options"]["pool_size"] == 1
    assert settings["engine_options"]["max_overflow"] == 0
    assert settings["engine_options"]["pool_timeout"] == 10
    assert settings["engine_options"]["connect_args"] == {"connect_timeout": 10}
    assert settings["trusted_hosts"] == ["exams.example.edu", ".vercel.app"]


def test_vercel_production_allows_deployment_subdomains(tmp_path, client, monkeypatch):
    settings = application._database_configuration(
        tmp_path,
        {
            "VERCEL": "1",
            "VERCEL_ENV": "production",
            "DATABASE_URL": "postgres://user:pass@example.test/exams",
        },
    )

    assert settings["trusted_hosts"] == [".vercel.app"]
    monkeypatch.setitem(app.config, "TRUSTED_HOSTS", settings["trusted_hosts"])
    response = client.get(
        "/",
        headers={"Host": "online-examination-system-preview.vercel.app"},
    )
    assert response.status_code == 200


def test_vercel_preview_uses_production_database_requirements(tmp_path):
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        application._database_configuration(
            tmp_path,
            {"VERCEL": "1", "VERCEL_ENV": "preview"},
        )

    local_vercel_settings = application._database_configuration(
        tmp_path,
        {"VERCEL": "1", "VERCEL_ENV": "development"},
    )
    assert not local_vercel_settings["production"]


def test_neon_postgresql_url_uses_psycopg_and_preserves_ssl_options(tmp_path):
    settings = application._database_configuration(
        tmp_path,
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://exam_user:fake-password@ep-example.neon.tech/exams?sslmode=require",
        },
    )
    url = make_url(settings["database_url"])

    assert url.drivername == "postgresql+psycopg"
    assert url.host == "ep-example.neon.tech"
    assert url.database == "exams"
    assert url.query["sslmode"] == "require"


def test_production_database_configuration_rejects_variable_name_as_url(tmp_path):
    with pytest.raises(RuntimeError, match="complete PostgreSQL connection URI"):
        application._database_configuration(
            tmp_path,
            {"APP_ENV": "production", "DATABASE_URL": "DATABASE_URL"},
        )


def test_production_secret_key_does_not_require_instance_filesystem(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    instance_path = tmp_path / "read-only-instance"

    assert application._secret_key(instance_path, production=True) == "a" * 48
    assert not instance_path.exists()


def test_production_secret_key_requires_a_private_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    instance_path = tmp_path / "instance"

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        application._secret_key(instance_path, production=True)
    assert not instance_path.exists()


def test_static_assets_are_available_from_vercel_public_directory(client):
    response = client.get("/static/css/style.css")
    assert response.status_code == 200
    assert b".topbar" in response.data


@pytest.fixture(autouse=True)
def database():
    app.config.update(TESTING=True)
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


def create_user(name, email, role, password="correct-horse-battery"):
    with app.app_context():
        user = User(
            name=name,
            email=email,
            role=role,
            password_hash=application.generate_password_hash(password),
            roll_number="ST-100" if role == "student" else None,
            department="Computer Science" if role == "student" else None,
            stream="B.Tech" if role == "student" else None,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def csrf_for(client):
    with client.session_transaction() as browser_session:
        return browser_session["_csrf_token"]


def begin_attempt(client, attempt_id):
    return client.post(
        f"/api/attempts/{attempt_id}/begin",
        json={},
        headers={"X-CSRF-Token": csrf_for(client)},
    )


def login(client, email, password="correct-horse-battery"):
    client.get("/login")
    response = client.post(
        "/login",
        data={"csrf_token": csrf_for(client), "email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 302
    return response


def create_exam(teacher_id, title="Foundations of computing"):
    with app.app_context():
        exam = Exam(
            title=title,
            subject="Computer Science",
            description="Choose the best answer.",
            duration_minutes=30,
            total_marks=5,
            created_by=teacher_id,
            status="published",
        )
        question_one = Question(question_text="What is 2 + 2?", marks=2)
        question_one.options = [
            Option(option_text="3", is_correct=False),
            Option(option_text="4", is_correct=True),
            Option(option_text="5", is_correct=False),
            Option(option_text="6", is_correct=False),
        ]
        question_two = Question(question_text="Which is a primary color?", marks=3)
        question_two.options = [
            Option(option_text="Green", is_correct=False),
            Option(option_text="Blue", is_correct=True),
            Option(option_text="Purple", is_correct=False),
            Option(option_text="Black", is_correct=False),
        ]
        exam.questions = [question_one, question_two]
        db.session.add(exam)
        db.session.commit()
        return exam.id, question_one.id, question_one.options[1].id, question_two.id, question_two.options[1].id


def test_first_admin_setup_is_one_time_and_csrf_protected(client):
    response = client.get("/setup/first-admin")
    assert response.status_code == 200
    token = csrf_for(client)
    response = client.post(
        "/setup/first-admin",
        data={
            "csrf_token": token,
            "name": "Portal Admin",
            "email": "admin@example.test",
            "password": "a-very-long-password",
        },
    )
    assert response.status_code == 302
    assert client.get("/setup/first-admin").status_code == 404

    client.get("/login")
    response = client.post("/login", data={"email": "admin@example.test", "password": "a-very-long-password"})
    assert response.status_code == 400
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_for(client),
            "email": "admin@example.test",
            "password": "a-very-long-password",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/admin")


def test_public_admin_setup_is_disabled_in_production(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/setup/first-admin").status_code == 404


def test_role_specific_login_routes_work_for_teacher_and_admin():
    teacher_client = app.test_client()
    admin_client = app.test_client()

    create_user("Teacher", "teacher@example.test", "teacher", "teacher-password")
    create_user("Admin", "admin@example.test", "admin", "admin-password")

    teacher_client.get("/teacher/login")
    teacher_response = teacher_client.post(
        "/teacher/login",
        data={
            "csrf_token": csrf_for(teacher_client),
            "email": "teacher@example.test",
            "password": "teacher-password",
        },
        follow_redirects=False,
    )
    assert teacher_response.status_code == 302
    assert teacher_response.location.endswith("/teacher")

    admin_client.get("/admin/login")
    admin_response = admin_client.post(
        "/admin/login",
        data={
            "csrf_token": csrf_for(admin_client),
            "email": "admin@example.test",
            "password": "admin-password",
        },
        follow_redirects=False,
    )
    assert admin_response.status_code == 302
    assert admin_response.location.endswith("/admin")


def test_admin_can_create_staff_and_deactivate_account(client):
    admin_id = create_user("Admin", "admin@example.test", "admin")
    login(client, "admin@example.test")
    token = csrf_for(client)
    response = client.post(
        "/admin/users",
        data={
            "csrf_token": token,
            "name": "Exam Teacher",
            "email": "teacher@example.test",
            "password": "temporary-password",
            "role": "teacher",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        teacher = db.session.scalar(select(User).where(User.email == "teacher@example.test"))
        teacher_id = teacher.id
    assert client.get(f"/admin/users/{teacher_id}/status", follow_redirects=False).status_code == 405
    response = client.post(
        f"/admin/users/{teacher_id}/status",
        data={"csrf_token": csrf_for(client)},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, teacher_id).status == "inactive"
        assert db.session.get(User, admin_id).status == "active"


def test_admin_can_reset_password_and_delete_account(client):
    admin_id = create_user("Admin", "admin@example.test", "admin")
    target_id = create_user("Target Teacher", "target@example.test", "teacher")
    login(client, "admin@example.test")

    response = client.post(
        f"/admin/users/{target_id}/password",
        data={"csrf_token": csrf_for(client), "password": "new-temporary-password"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        target = db.session.get(User, target_id)
        assert application.check_password_hash(target.password_hash, "new-temporary-password")
        assert db.session.get(User, admin_id).status == "active"

    response = client.post(
        f"/admin/users/{target_id}/delete",
        data={"csrf_token": csrf_for(client)},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, target_id) is None
        assert db.session.get(User, admin_id) is not None


def test_student_registration_and_role_dashboards(client):
    client.get("/register")
    response = client.post(
        "/register",
        data={
            "csrf_token": csrf_for(client),
            "name": "New Student",
            "email": "new-student@example.test",
            "password": "student-password",
            "roll_number": "CS-102",
            "department": "Computer Science",
            "stream": "B.Tech",
        },
    )
    assert response.status_code == 302
    login(client, "new-student@example.test", "student-password")
    assert client.get("/student").status_code == 200
    assert client.get("/admin").status_code == 403
    assert client.get("/teacher").status_code == 403


def test_teacher_can_create_scheduled_exam_and_question_set(client):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    login(client, "teacher@example.test")
    assert client.get("/teacher").status_code == 200
    response = client.post(
        "/teacher/exams/new",
        data={
            "csrf_token": csrf_for(client),
            "title": "Scheduled examination",
            "subject": "Computer Science",
            "department": "Computer Science",
            "stream": "B.Tech",
            "topics": "algorithms, data structures",
            "target_question_count": "10",
            "description": "Review each question.",
            "duration_minutes": "45",
            "total_marks": "2",
            "start_at": "2035-07-01T10:00:00+05:30",
            "end_at": "2035-07-01T11:00:00+05:30",
            "status": "published",
        },
    )
    assert response.status_code == 302
    assert "/questions" in response.location
    questions_page = client.get(response.location)
    assert questions_page.status_code == 200
    exam_id = int(response.location.split("/")[3])
    response = client.post(
        response.location,
        data={
            "csrf_token": csrf_for(client),
            "question_text": "Which answer is correct?",
            "options": ["First", "Second", "Third", "Fourth"],
            "correct_index": "1",
            "marks": "2",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        exam = db.session.get(Exam, exam_id)
        assert exam.created_by == teacher_id
        assert exam.department == "Computer Science"
        assert exam.stream == "B.Tech"
        assert exam.target_question_count == 10
        assert exam.start_at.isoformat() == "2035-07-01T04:30:00"
        assert len(exam.questions) == 1
        assert exam.questions[0].options[1].is_correct
        question_id = exam.questions[0].id
    response = client.post(
        f"/teacher/questions/{question_id}/delete",
        data={"csrf_token": csrf_for(client)},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Question, question_id) is None
    assert client.get("/teacher/results").status_code == 200


def test_ai_question_generation_adds_validated_mcqs(client, monkeypatch):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    login(client, "teacher@example.test")
    response = client.post(
        "/teacher/exams/new",
        data={
            "csrf_token": csrf_for(client),
            "title": "AI algorithms test",
            "subject": "Computer Science",
            "department": "Computer Science",
            "stream": "B.Tech",
            "topics": "sorting, searching",
            "target_question_count": "2",
            "duration_minutes": "30",
            "total_marks": "4",
            "status": "published",
        },
    )
    exam_id = int(response.location.split("/")[3])
    ai_questions = [
        {
            "question_text": "Which algorithm sorts by repeatedly swapping adjacent items?",
            "options": ["Bubble sort", "Binary search", "Merge sort", "Hashing"],
            "correct_index": 0,
        },
        {
            "question_text": "Which search requires sorted input?",
            "options": ["Linear search", "Binary search", "Depth-first search", "Hash lookup"],
            "correct_index": 1,
        },
    ]
    monkeypatch.setattr(application, "_generate_ai_questions", lambda exam, count: ai_questions)
    response = client.post(
        f"/teacher/exams/{exam_id}/generate",
        data={"csrf_token": csrf_for(client), "question_count": "2"},
    )
    assert response.status_code == 302
    with app.app_context():
        exam = db.session.get(Exam, exam_id)
        assert exam.created_by == teacher_id
        assert len(exam.questions) == 2
        assert exam.status == "draft"
        assert exam.target_question_count == 2
        assert sum((Decimal(question.marks) for question in exam.questions), Decimal("0")) == Decimal("4")
        assert exam.questions[0].options[0].is_correct
        assert len(exam.questions[1].options) == 4


def test_gemini_question_generator_parses_generate_content_json(monkeypatch):
    question_data = {
        "questions": [
            {
                "question_text": "Which structure uses FIFO order?",
                "options": ["Queue", "Stack", "Tree", "Graph"],
                "correct_index": 0,
            }
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": json.dumps(question_data)}]}}]}
            ).encode("utf-8")

    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    requests = []

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(application, "urlopen", fake_urlopen)
    exam = SimpleNamespace(
        department="Computer Science",
        stream="B.Tech",
        subject="Data structures",
        topics="queues",
    )

    assert application._generate_ai_questions(exam, 1) == question_data["questions"]
    assert requests[0].full_url.endswith("/models/gemini-3.6-flash:generateContent")
    assert requests[0].get_header("X-goog-api-key") == "test-api-key"


def test_gemini_quota_error_explains_http_429(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    def fail_with_quota(*_args, **_kwargs):
        raise application.HTTPError("https://example.test", 429, "quota", {}, io.BytesIO())

    monkeypatch.setattr(application, "urlopen", fail_with_quota)
    exam = SimpleNamespace(
        department="Computer Science",
        stream="B.Tech",
        subject="Data structures",
        topics="queues",
    )

    with pytest.raises(RuntimeError, match="Gemini rate limit or API quota"):
        application._generate_ai_questions(exam, 1)


def test_exam_visibility_is_limited_to_matching_student_department_and_stream(client):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    matching_exam = create_exam(teacher_id, "Computer science exam")[0]
    other_exam = create_exam(teacher_id, "Other department exam")[0]
    other_stream_exam = create_exam(teacher_id, "Other stream exam")[0]
    all_students_exam = create_exam(teacher_id, "All students exam")[0]
    with app.app_context():
        db.session.get(Exam, matching_exam).department = "Computer Science"
        db.session.get(Exam, matching_exam).stream = "B.Tech"
        db.session.get(Exam, other_exam).department = "Electrical Engineering"
        db.session.get(Exam, other_exam).stream = "B.Tech"
        db.session.get(Exam, other_stream_exam).department = "Computer Science"
        db.session.get(Exam, other_stream_exam).stream = "B.Sc"
        db.session.get(Exam, all_students_exam).department = "All departments"
        db.session.get(Exam, all_students_exam).stream = "All streams"
        db.session.commit()
    create_user("Student", "student@example.test", "student")
    login(client, "student@example.test")
    response = client.get("/student")
    assert b"Computer science exam" in response.data
    assert b"Other department exam" not in response.data
    assert b"Other stream exam" not in response.data
    assert b"All students exam" in response.data
    assert client.get(f"/student/exams/{other_exam}/instructions").status_code == 404
    assert client.get(f"/student/exams/{other_stream_exam}/instructions").status_code == 404


def test_student_can_attempt_autosave_and_receive_server_calculated_result(client):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    exam_id, first_question, correct_first, second_question, correct_second = create_exam(teacher_id)
    create_user("Student", "student@example.test", "student")
    login(client, "student@example.test")

    assert client.get(f"/student/exams/{exam_id}/instructions").status_code == 200
    started = client.post(
        f"/student/exams/{exam_id}/start",
        data={"csrf_token": csrf_for(client)},
    )
    assert started.status_code == 302
    attempt_id = int(started.location.rstrip("/").split("/")[-1])

    page = client.get(started.location)
    assert page.status_code == 200
    assert b"Camera preview stays on this device" in page.data
    assert b"is_correct" not in page.data
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.started_at is None
        assert attempt.deadline_at is None

    activated = begin_attempt(client, attempt_id)
    assert activated.status_code == 200
    assert activated.json["status"] == "started"
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.started_at is not None
        assert attempt.deadline_at is not None

    token = csrf_for(client)
    saved = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"question_id": first_question, "option_id": correct_first},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200
    assert saved.json["status"] == "saved"

    invalid_option = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"question_id": second_question, "option_id": correct_first},
        headers={"X-CSRF-Token": token},
    )
    assert invalid_option.status_code == 400

    submitted = client.post(
        f"/student/attempts/{attempt_id}/submit",
        data={"csrf_token": token, f"q_{second_question}": str(correct_second)},
    )
    assert submitted.status_code == 302
    result = client.get(submitted.location)
    assert result.status_code == 200
    assert b"5.00" in result.data

    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.submitted_at is not None
        assert float(attempt.score) == 5
        assert len(attempt.answers) == 2
        assert all(answer.is_correct for answer in attempt.answers)

    repeated = client.post(
        f"/student/attempts/{attempt_id}/submit",
        data={"csrf_token": token},
    )
    assert repeated.status_code == 302
    with app.app_context():
        assert db.session.scalar(select(func.count(Attempt.id))) == 1

    start_again = client.post(
        f"/student/exams/{exam_id}/start",
        data={"csrf_token": token},
    )
    assert start_again.status_code == 302
    assert start_again.location.endswith(f"/student/results/{attempt_id}")

    teacher_client = app.test_client()
    login(teacher_client, "teacher@example.test")
    assert teacher_client.get(f"/teacher/exams/{exam_id}/edit").status_code == 302
    locked = teacher_client.post(
        f"/teacher/exams/{exam_id}/questions",
        data={
            "csrf_token": csrf_for(teacher_client),
            "question_text": "A late question",
            "options": ["A", "B", "C", "D"],
            "correct_index": "0",
            "marks": "1",
        },
    )
    assert locked.status_code == 302
    with app.app_context():
        assert len(db.session.get(Exam, exam_id).questions) == 2


def test_expired_attempt_auto_submits_saved_answers(client, monkeypatch):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    exam_id, question_id, correct_option, _second_question, _second_correct = create_exam(teacher_id)
    create_user("Student", "student@example.test", "student")
    login(client, "student@example.test")
    started = client.post(
        f"/student/exams/{exam_id}/start",
        data={"csrf_token": csrf_for(client)},
    )
    attempt_id = int(started.location.rstrip("/").split("/")[-1])
    assert begin_attempt(client, attempt_id).status_code == 200
    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"question_id": question_id, "option_id": correct_option},
        headers={"X-CSRF-Token": csrf_for(client)},
    )
    assert response.status_code == 200
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        expired_at = attempt.deadline_at + timedelta(seconds=1)

    monkeypatch.setattr(application, "utcnow", lambda: expired_at)
    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"question_id": question_id, "option_id": correct_option},
        headers={"X-CSRF-Token": csrf_for(client)},
    )
    assert response.status_code == 200
    assert response.json["status"] == "submitted"
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.submitted_at is not None
        assert attempt.submission_reason == "time_expired"
        assert float(attempt.score) == 2
        assert len(attempt.answers) == 2
        assert any(answer.selected_option_id is None for answer in attempt.answers)


def test_three_proctoring_violations_submit_attempt_and_record_reason(client):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    exam_id, question_id, correct_option, _second_question, _second_correct = create_exam(teacher_id)
    create_user("Student", "student@example.test", "student")
    login(client, "student@example.test")
    started = client.post(
        f"/student/exams/{exam_id}/start",
        data={"csrf_token": csrf_for(client)},
    )
    attempt_id = int(started.location.rstrip("/").split("/")[-1])

    pending_violation = client.post(
        f"/api/attempts/{attempt_id}/proctoring-violations",
        json={"event_type": "tab_switch"},
        headers={"X-CSRF-Token": csrf_for(client)},
    )
    assert pending_violation.status_code == 409
    assert begin_attempt(client, attempt_id).status_code == 200

    saved = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"question_id": question_id, "option_id": correct_option},
        headers={"X-CSRF-Token": csrf_for(client)},
    )
    assert saved.status_code == 200
    token = csrf_for(client)

    for count, event_type in enumerate(
        ["tab_switch", "fullscreen_exit", "clipboard_attempt"], start=1
    ):
        response = client.post(
            f"/api/attempts/{attempt_id}/proctoring-violations",
            json={"event_type": event_type},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200
        assert response.json["violations"] == count
        if count < 3:
            assert response.json["status"] == "recorded"
            assert response.json["remaining"] == 3 - count
        else:
            assert response.json["status"] == "submitted"
            assert response.json["redirect"].endswith(f"/student/results/{attempt_id}")

    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.submitted_at is not None
        assert attempt.proctoring_violations == 3
        assert attempt.submission_reason == "proctoring"
        assert float(attempt.score) == 2
        assert len(attempt.answers) == 2
    result_page = client.get(response.json["redirect"])
    assert result_page.status_code == 200
    assert b"submitted automatically after three proctoring violations" in result_page.data


def test_proctoring_violation_rejects_unknown_event_type(client):
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    exam_id, _question_id, _correct_option, _second_question, _second_correct = create_exam(teacher_id)
    create_user("Student", "student@example.test", "student")
    login(client, "student@example.test")
    started = client.post(
        f"/student/exams/{exam_id}/start",
        data={"csrf_token": csrf_for(client)},
    )
    attempt_id = int(started.location.rstrip("/").split("/")[-1])
    assert begin_attempt(client, attempt_id).status_code == 200

    response = client.post(
        f"/api/attempts/{attempt_id}/proctoring-violations",
        json={"event_type": "made_up_event"},
        headers={"X-CSRF-Token": csrf_for(client)},
    )
    assert response.status_code == 400
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.proctoring_violations == 0


def test_php_bcrypt_password_is_upgraded_after_successful_login(client):
    with app.app_context():
        legacy_hash = bcrypt.hashpw(b"legacy-password", bcrypt.gensalt()).decode()
        legacy_hash = legacy_hash.replace("$2b$", "$2y$", 1)
        user = User(name="Legacy Student", email="legacy@example.test", role="student", password_hash=legacy_hash)
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_for(client),
            "email": "legacy@example.test",
            "password": "legacy-password",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, user_id).password_hash.startswith("scrypt:")


def test_teacher_cannot_manage_another_teachers_exam(client):
    owner_id = create_user("Owner", "owner@example.test", "teacher")
    other_teacher = create_user("Other", "other@example.test", "teacher")
    exam_id, *_ = create_exam(owner_id)
    login(client, "other@example.test")
    assert client.get(f"/teacher/exams/{exam_id}/questions").status_code == 403
    assert client.get(f"/teacher/exams/{exam_id}/edit").status_code == 403


def test_admin_and_student_result_pages_render(client):
    admin_id = create_user("Admin", "admin@example.test", "admin")
    teacher_id = create_user("Teacher", "teacher@example.test", "teacher")
    exam_id, _question_id, _correct, _second_question, _second_correct = create_exam(teacher_id)
    student_id = create_user("Student", "student@example.test", "student")

    login(client, "admin@example.test")
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/users").status_code == 200
    assert client.get("/teacher").status_code == 200
    assert client.get("/teacher/exams/new").status_code == 200

    client.post("/logout", data={"csrf_token": csrf_for(client)})
    login(client, "student@example.test")
    assert client.get("/student").status_code == 200
    assert client.get("/student/results").status_code == 200
    with app.app_context():
        assert db.session.get(User, admin_id).role == "admin"
        assert db.session.get(User, student_id).role == "student"
        assert db.session.get(Exam, exam_id).created_by == teacher_id


def test_mysql_importer_copies_existing_schema_without_source_changes(tmp_path, monkeypatch):
    from scripts import import_mysql

    source_engine = create_engine(
        URL.create("sqlite", database=str(tmp_path / "legacy.sqlite3"))
    )
    db.metadata.create_all(source_engine)
    with Session(source_engine) as source_session:
        teacher = User(
            name="Legacy Teacher",
            email="legacy-teacher@example.test",
            password_hash="legacy-password-hash",
            role="teacher",
        )
        exam = Exam(
            title="Imported examination",
            subject="Computer Science",
            duration_minutes=25,
            total_marks=2,
            created_by=1,
            status="published",
        )
        question = Question(question_text="Imported question", marks=2)
        question.options = [
            Option(option_text=f"Choice {index}", is_correct=index == 1)
            for index in range(4)
        ]
        exam.questions = [question]
        source_session.add(teacher)
        source_session.flush()
        exam.created_by = teacher.id
        source_session.add(exam)
        source_session.commit()

    monkeypatch.setenv(
        "SOURCE_DATABASE_URL",
        URL.create("sqlite", database=str(tmp_path / "legacy.sqlite3")).render_as_string(
            hide_password=False
        ),
    )
    import_mysql.main()

    with app.app_context():
        imported = db.session.scalar(select(User).where(User.email == "legacy-teacher@example.test"))
        assert imported is not None
        assert imported.password_hash == "legacy-password-hash"
        assert db.session.scalar(select(Exam).where(Exam.title == "Imported examination")) is not None
        assert db.session.scalar(select(Question).where(Question.question_text == "Imported question")) is not None

    source_engine.dispose()
