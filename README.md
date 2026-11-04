# Online Examination System

A Flask examination portal with separate administrator, teacher, and student
workspaces. It supports account management, scheduled multiple-choice exams,
timed attempts, autosaved answers, server-side scoring, and result history.
The existing features and database models are retained.

## Requirements

- Python 3.10 or later for local development
- Python 3.12 for Vercel (pinned in `.python-version`)
- SQLite for local development (included with Python)
- A persistent PostgreSQL database for production
- Vercel project/account for hosting

## Local development (SQLite)

From the repository root, create a virtual environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

By default, the app uses `instance/college_exam_system.sqlite3`. You can leave
`DATABASE_URL` unset or use `DATABASE_URL=sqlite:///college_exam_system.sqlite3`;
relative SQLite paths are resolved inside `instance/`. Set a private
`SECRET_KEY` in `.env` if you need sessions to remain valid across local
restarts. Generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Apply database migrations and provision the initial administrator:

```powershell
flask --app app db upgrade
flask --app app create-admin
flask --app app run
```

Open <http://127.0.0.1:5000/>. The `/setup/first-admin` page remains available
only in local development and only until an administrator exists.

If `instance/college_exam_system.sqlite3` already exists from an earlier
release, do not run the fresh-database migration command above first. Back it
up and confirm that its tables/columns match the checked-in initial migration
before marking it as migrated:

```powershell
flask --app app db stamp head
flask --app app db upgrade
```

Do not stamp an empty or incompatible database. `flask --app app init-db` is a
convenience alias for applying migrations to a fresh database.

Run tests with:

```powershell
python -m pytest
```

## Vercel deployment

The repository root is the Vercel project root. The root `app.py` exports the
Flask WSGI object as `app`, which Vercel's Python runtime imports; the
development server is only started when `app.py` is run directly. `.python-version`
pins the Vercel runtime to Python 3.12. `vercel.json` configures the function
duration and includes Jinja templates; files in `public/` are served as static
assets by Vercel's CDN. Leave the build command and output directory at their
Vercel defaults; no separate build step or container is required.

### 1. Provision production services and settings

Create a PostgreSQL database reachable from Vercel (use its TLS-enabled
connection URL; a provider's connection pooler is recommended when available).
In Vercel Project Settings → Environment Variables, configure for **Production**
and any Preview environments you intend to use:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | A private random value of at least 32 characters |
| `DATABASE_URL` | PostgreSQL connection URL from your database provider |
| `TRUSTED_HOSTS` | Optional comma-separated hosts for custom domains; defaults to `*.vercel.app` |

`SECRET_KEY` and `DATABASE_URL` are required for every Vercel deployment
environment. `APP_ENV=production` is recommended; Vercel production and preview
environments are also recognized automatically. The application intentionally
fails at import when its production secret or persistent PostgreSQL URL is
missing. That startup failure is surfaced by Vercel as a function invocation
error, so configure these variables before testing a deployment. Do not enter
example or local SQLite URLs as production database credentials.

For Neon, set `DATABASE_URL` to the **complete connection URI copied from
Neon's dashboard**, not the text `DATABASE_URL`, a placeholder, or a hostname
alone. It must use a PostgreSQL scheme such as `postgresql://` (Neon's URI
typically includes `sslmode=require`). The application converts that scheme to
the installed Psycopg 3 SQLAlchemy driver. Do not paste the connection URI or
password into source files, commits, or chat.

Generate a secret locally without sharing it:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Do not put production credentials in `.env.example`, source control, build
logs, or client-side variables. The app normalizes `postgres://` and
`postgresql://` URLs to the installed Psycopg 3 driver. Production startup
fails closed if the secret or database URL is missing, if the key is too short,
or if SQLite is selected. Vercel preview deployments also require PostgreSQL;
use an isolated preview database if you need preview data separation.

### 2. Apply migrations and create the first administrator

Run these commands from a trusted local machine or CI job that has network
access to the production database, with `APP_ENV`, `SECRET_KEY`, and
`DATABASE_URL` supplied through a secure environment/secret manager:

```powershell
flask --app app db upgrade
flask --app app create-admin
```

The admin command prompts for the initial account details and password. The
public first-admin setup page is disabled in production. Run migrations once
per release, before directing traffic to a schema that depends on them; do not
run schema changes from each serverless request.

For an existing, schema-compatible production database that predates Alembic,
take a verified backup and use `flask --app app db stamp head` instead of
`db upgrade` for the initial baseline, then use `db upgrade` for later
migrations. Confirm the schema matches the baseline first; stamping does not
change or validate table structure.

### 3. Connect and deploy

Import this repository into Vercel, select the `main` branch, and keep the
project root set to this repository root (not a subfolder). Use the default
Python/Other framework detection, leave Build Command and Output Directory
unset, and keep `vercel.json`, `.python-version`, and `requirements.txt` in the
root. Deploy using the Vercel Git integration or CLI:

```powershell
vercel
vercel --prod
```

The repository does not contain Vercel credentials, a PostgreSQL service
connection, or access to Vercel project settings. Configure the environment
variables and database in their respective provider dashboards, then deploy.
Apply migrations once from a trusted environment before sending traffic.
Validate the production URL, login, exam creation/attempt flows, static assets,
logs, and database connectivity after deployment.

## Persistence, security, and operations

- **Do not use SQLite as the Vercel production database.** Function filesystems
  are ephemeral and may be isolated between invocations. PostgreSQL is the
  durable system of record.
- Use a private `SECRET_KEY` shared consistently across function instances;
  otherwise signed browser sessions will be invalidated. Rotate it deliberately
  because rotation signs users out.
- Production enables secure, HTTP-only, same-site session cookies and existing
  CSRF/security-header protections. Configure `TRUSTED_HOSTS` for custom host
  names; the default production allowlist covers Vercel subdomains.
- Each PostgreSQL function instance keeps a small connection pool. A managed
  pooler can help under scale-out; the application limits each instance to one
  pooled connection, uses a 10-second pool/connect timeout, and recycles
  connections. Review provider connection limits and configure the database
  region close to the Vercel function region.
- Vercel's request/response functions are not persistent workers. Long exams
  are supported by storing attempt state in PostgreSQL and autosaving answers;
  no in-memory state is relied on.
- Use Vercel runtime logs and the PostgreSQL provider's monitoring to diagnose
  failures. Never log database URLs, passwords, session values, or exam secrets.

## Database migrations

Alembic revisions live in `migrations/versions/`. After changing models, create
and review a migration locally:

```powershell
flask --app app db migrate -m "Describe schema change"
flask --app app db upgrade
```

Commit reviewed migration scripts with the corresponding model changes. Schema
changes are never automatically created on a web request.

## Existing capabilities

- Admin: account provisioning/status, portal statistics, examination and
  result visibility.
- Teacher: exam scheduling and publication, four-choice question sets, and
  result review.
- Student: registration, scheduled exams, timed attempts, autosave, automatic
  deadline submission, and results.
- Session-bound CSRF validation, role checks, password hashing/legacy bcrypt
  verification, secure production cookies, and security headers.
- `scripts/import_mysql.py` remains available for importing the legacy MySQL
  schema after taking backups and configuring its source connection.
