# Deployment modernization summary

## Result

Prepared the existing Flask examination system for Vercel without creating a
second project or performing a deployment. Existing administrator, teacher,
student, exam, timed attempt, autosave, scoring, authentication, and CSRF
features remain in place.

## Artifacts and configuration

- Added `vercel.json` using the Flask `app.py` entry point, a 30-second
  function duration, and template inclusion.
- Moved the existing CSS and JavaScript into `public/static/` for Vercel's
  static asset handling; the app serves that same directory during local
  development.
- Updated `app.py` to default to an instance-local SQLite database locally,
  resolve relative SQLite URLs into `instance/`, normalize PostgreSQL URLs to
  Psycopg 3, tune PostgreSQL connection pooling, and require PostgreSQL plus a
  32-character secret in production/preview environments.
- Removed request-time `create_all()` and changed `init-db` to apply
  migrations. Preserved security controls and added production Host allowlist
  defaults/configuration.
- Added Flask-Migrate, Psycopg 3, and an Alembic baseline for all six existing
  tables.
- Updated `.env.example`, ignored `.vercel/`, added migration/Vercel setup and
  persistence guidance to `README.md`, and added configuration/static-asset
  tests.

## Verification

- Existing suite before edits: `11 passed`.
- Final `python -m compileall -q app.py models.py migrations tests`: passed.
- Final `python -m pytest -q`: `15 passed`.
- `vercel.json` JSON parsing, Flask import, static path, and route-map checks:
  passed.
- Production configuration checks: missing database rejected, SQLite rejected,
  PostgreSQL configuration accepted with secure cookies, and Vercel preview
  without PostgreSQL rejected.
- `flask --app app db upgrade` and the `init-db` alias both applied the initial
  migration to disposable SQLite databases; Alembic reported revision
  `eca7f2e56ee3 (head)`. Temporary databases were removed.
- Existing local SQLite data and generated secret were not used for migration
  tests; the initial worktree was clean.

## Caveats and blockers

- No Vercel deployment or live PostgreSQL connection was attempted. Vercel
  project settings, a reachable persistent PostgreSQL service, production
  secret, migration execution, and production smoke tests remain operator
  steps.
- Existing databases created before Alembic must be backed up and checked for
  baseline schema compatibility before `flask db stamp head`; stamping does
  not validate or alter the schema.
- Vercel's filesystem is not durable; SQLite is intentionally refused in
  production.
- The Vercel CLI was not used for deployment validation.
