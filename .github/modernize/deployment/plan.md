# Deployment modernization plan

## Goal and scope

Prepare the existing Flask college examination system for Vercel, using SQLite
for local development and PostgreSQL for production. Preserve the existing
administrator, teacher, student, exam, timed-attempt, autosave, scoring,
authentication, and CSRF-protected workflows. Do not deploy the application,
modify unrelated application behavior, or include secrets.

## Repository findings

- The repository is a single Python/Flask application (`app.py`, `models.py`)
  with Jinja templates, static assets, SQLAlchemy models, and 11 passing tests.
- `DATABASE_URL` can select SQLite, but the request hook currently runs
  `db.create_all()` lazily, which is not an appropriate migration strategy for
  serverless PostgreSQL.
- The current runtime dependencies omit a PostgreSQL driver and migration
  tooling; no Vercel configuration or function entry point exists.
- Existing security controls include session-bound CSRF checks, HTTP-only
  cookies, same-site cookies, production secure cookies, security headers, and
  validation for a production `SECRET_KEY`. Preserve these controls.
- The initial worktree was clean on `main`; existing ignored local runtime
  files, including the SQLite database and generated development secret, are
  not to be added or modified.

## Deployment type and design

- Vercel Python serverless function serving the existing Flask WSGI `app`.
- Root-level Vercel configuration and app entry point; no second project.
- Local mode defaults to SQLite under `instance/`.
- Production mode requires a strong secret and a PostgreSQL `DATABASE_URL`,
  normalized for the selected Psycopg 3 driver. Never use Vercel's ephemeral
  filesystem SQLite as the production database.
- Apply schema changes using Flask-Migrate/Alembic (`flask db upgrade`) rather
  than creating tables on web requests. Document how to initialize a fresh
  database and how to stamp a pre-existing, schema-compatible database.
- Keep current features and the existing MySQL import utility intact.

## Planned files

1. `vercel.json` — configure the Flask function and include runtime templates.
2. `public/static/` — move the existing static CSS and JavaScript into
   Vercel's CDN-served `public/` tree without changing their contents.
3. `app.py` — production-aware environment/database validation, SQLAlchemy
   PostgreSQL connection settings, migration initialization, and remove
   per-request schema creation.
4. `requirements.txt` — add Flask-Migrate and Psycopg 3 runtime dependencies.
5. `migrations/` — checked-in Alembic environment and an initial schema
   revision compatible with the existing SQLAlchemy models.
6. `.env.example` — document local SQLite and production PostgreSQL settings
   without real credentials.
7. `.gitignore` — exclude deployment-local and migration-generated temporary
   files if needed, without changing user data.
8. `README.md` — replace or extend setup guidance with SQLite development,
   migrations, Vercel settings/deployment steps, admin provisioning,
   troubleshooting, and persistence/security caveats.
9. `tests/test_app.py` — add focused tests for environment defaults,
   PostgreSQL URL normalization/production requirements, and migration-safe
   startup behavior without removing current feature tests.
10. `.github/modernize/deployment/plan.md` and `progress.md` — required task
   tracking artifacts.

No application source feature redesign, secrets, live deployment, or commit is
within the user-requested scope.

## Execution and validation steps

1. Confirm the tracked worktree is clean and preserve ignored user runtime data.
2. Add Vercel routing and WSGI entry point without restructuring the project.
3. Configure local SQLite and production PostgreSQL / Psycopg 3; fail fast on
   a missing production secret/database or a SQLite production URL.
4. Replace request-time `create_all()` with explicit Flask-Migrate commands;
   add and inspect the initial migration against all six models.
5. Document environment variables, PostgreSQL provider requirements, Vercel
   configuration, migration procedures, admin creation, and the non-persistent
   nature of serverless local files.
6. Run Python syntax compilation, import/configuration checks, route-map and
   health checks, and the complete pytest suite. Where feasible, exercise
   Alembic upgrade against a temporary SQLite database; PostgreSQL/Vercel live
   deployment checks require external services and are not attempted.
7. Inspect final changed-file list and ensure no generated database, secret,
   or unrelated file was touched. Record final results and caveats in
   `progress.md` and `summary.md`.

## Validation criteria

- Vercel configuration parses and exposes the Flask application as a Python
  function with its Jinja and static files.
- Development configuration defaults to an instance-local SQLite database.
- Production configuration requires a strong `SECRET_KEY`, a PostgreSQL URL,
  secure cookies, and supported driver; it refuses SQLite production storage.
- Fresh schemas can be applied through checked-in migrations; existing
  schema-compatible deployments have a documented stamp-and-upgrade path.
- Existing tests and new configuration/migration tests pass.
- No production deployment is performed and no credentials are committed.
