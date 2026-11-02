"""Copy the original MySQL database into the configured SQLite database."""

import os
import sys

from sqlalchemy import MetaData, Table, create_engine, func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from models import Answer, Attempt, Exam, Option, Question, User, db


TABLES = {
    "users": User,
    "exams": Exam,
    "questions": Question,
    "options": Option,
    "attempts": Attempt,
    "answers": Answer,
}


def main():
    source_url = os.environ.get("SOURCE_DATABASE_URL")
    if not source_url:
        raise SystemExit("Set SOURCE_DATABASE_URL to the existing MySQL connection URL.")

    with app.app_context():
        target_url = db.engine.url.render_as_string(hide_password=True)
        if source_url == target_url:
            raise SystemExit("Source and destination databases must be different.")

        db.create_all()
        for model in TABLES.values():
            if db.session.scalar(select(func.count()).select_from(model.__table__)):
                raise SystemExit(
                    f"Destination table {model.__tablename__!r} is not empty; import stopped without changes."
                )

        source = create_engine(source_url, pool_pre_ping=True)
        try:
            source_metadata = MetaData()
            source_metadata.reflect(bind=source, only=list(TABLES))
            missing = set(TABLES) - set(source_metadata.tables)
            if missing:
                raise SystemExit(
                    "The source database is missing tables: " + ", ".join(sorted(missing))
                )

            with source.connect() as source_connection, db.engine.begin() as target_connection:
                for table_name, model in TABLES.items():
                    source_table = Table(
                        table_name,
                        MetaData(),
                        autoload_with=source,
                    )
                    target_table = model.__table__
                    source_columns = {column.name for column in source_table.columns}
                    target_columns = {column.name for column in target_table.columns}
                    if source_columns != target_columns:
                        raise SystemExit(
                            f"Column mismatch in {table_name}: source={sorted(source_columns)}, "
                            f"target={sorted(target_columns)}"
                        )

                    rows = source_connection.execute(select(source_table)).mappings()
                    while batch := rows.fetchmany(500):
                        target_connection.execute(
                            target_table.insert(),
                            [dict(row) for row in batch],
                        )
                    print(f"Imported {table_name}.")
        finally:
            source.dispose()

    print("MySQL data copied to SQLite. The source database was not modified.")


if __name__ == "__main__":
    main()
