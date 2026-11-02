CAMPUS EXAMINATION SYSTEM
=========================

STACK
- HTML5, responsive CSS3, and vanilla JavaScript
- Python 3.10+ and Flask
- Flask-SQLAlchemy / SQLAlchemy
- SQLite (stored in instance/college_exam_system.sqlite3 by default)

QUICK START (WINDOWS)
1. Install Python 3.10 or later.
2. From this folder, create and activate a virtual environment:
     py -m venv .venv
     .venv\Scripts\activate
3. Install application dependencies:
     python -m pip install -r requirements.txt
4. Copy .env.example to .env. Set SECRET_KEY to a long, random value.
   Generate one with:
     python -c "import secrets; print(secrets.token_urlsafe(48))"
5. Initialize the database and create the first administrator:
     flask --app app init-db
     flask --app app create-admin
6. Start the development server:
     flask --app app run
7. Open http://127.0.0.1:5000/

Alternatively, in local development only, when no administrator exists, open
http://127.0.0.1:5000/setup/first-admin and create the initial account. That
setup page is disabled permanently as soon as the first admin is created and
is unavailable when APP_ENV=production; provision production admins with the
CLI command. The development server is for local use only. For a production deployment, set
APP_ENV=production, configure a private SECRET_KEY and persistent DATABASE_URL,
terminate TLS at a trusted reverse proxy, and run a production WSGI server.
For example, Waitress is included for a Windows deployment:
     waitress-serve --host=127.0.0.1 app:app

ROLES AND FEATURES
- Administrators create teacher/admin accounts, activate or deactivate accounts,
  view portal statistics, and manage examinations and results.
- Teachers create, edit, publish, schedule, and close examinations; manage
  four-choice MCQ questions; and review submitted scores.
- Students register, review exam instructions, attempt each exam once, see a
  server-authoritative timer, and view their submitted results.
- Student answers are validated against the current exam and autosaved to the
  database. Expired attempts auto-submit saved answers; score calculation and
  correctness checks run on the server.
- Every state-changing form and autosave request uses a session-bound CSRF token.

DATABASE AND EXISTING DATA
The SQLAlchemy models preserve the existing MySQL table and column names,
including the PHP password column, so the existing MySQL dump can be copied to
SQLite without changing exam/user IDs or answer history. To import a populated
MySQL database, first make a backup, install the included PyMySQL dependency,
and set SOURCE_DATABASE_URL to its SQLAlchemy connection URL, for example:
     set SOURCE_DATABASE_URL=mysql+pymysql://user:password@localhost/college_exam_system
Then run:
     python scripts\import_mysql.py
DATABASE_URL optionally selects the SQLite destination. Relative SQLite URLs
are resolved inside the instance/ folder (for example
sqlite:///college_exam_system.sqlite3). The importer refuses to write to a
non-empty destination, verifies the source tables/columns, and never modifies
the MySQL source. The original database/exam_system.sql remains as the legacy
MySQL schema reference.

TESTS
Run the application tests with:
     python -m pytest

The legacy PHP pages have been retired after migration and verification. The
original MySQL schema file remains available for the data-import workflow.
The Flask application starts with:
     flask --app app run
