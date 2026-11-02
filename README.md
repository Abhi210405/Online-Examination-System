# 📝 Online Examination System

A web-based platform for conducting online examinations, with Admin, Teacher, and Student modules for account and exam management, question handling, and result generation. The application provides separate workspaces for these roles, with exam timing and score calculation handled by the server.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Database](https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)

## 📚 Table of Contents

- [Project Overview](#-project-overview)
- [Key Features](#-key-features)
- [Admin Module](#-admin-module)
- [Teacher Module](#-teacher-module)
- [Student Module](#-student-module)
- [Technologies Used](#-technologies-used)
- [Project Structure](#-project-structure)
- [Installation and Setup](#-installation-and-setup)
- [How to Run the Project](#-how-to-run-the-project)
- [Tests](#-tests)
- [Database Import](#-database-import)
- [Future Enhancements](#-future-enhancements)
- [Author](#-author)
- [GitHub Repository](#-github-repository)

## 🔎 Project Overview

The Online Examination System is a Flask application for the day-to-day workflows of a college examination portal. Administrators manage staff accounts, teachers prepare and schedule exams, and students take available exams and review submitted results.

Exams use multiple-choice questions. During an attempt, student answers are saved as they are selected; the server enforces the deadline, validates answers, and calculates the final score. SQLite is used by default, and a utility is included to copy compatible data from the project’s existing MySQL schema into the application database.

## ✨ Key Features

- Separate, role-based dashboards for administrators, teachers, and students.
- Student registration and account sign-in.
- Exam creation and editing, with draft, published, and closed states.
- Exam availability schedules, question marks, and timed attempts.
- Four-choice multiple-choice questions.
- Answer autosave, question progress indicators, and automatic submission when time expires.
- Server-side answer validation and score calculation.
- One attempt per student for each exam.
- Session-bound CSRF checks on state-changing requests.
- SQLite by default, plus a MySQL-to-SQLite data import script.

## 🛡️ Admin Module

- View portal statistics from the administrator dashboard.
- Create teacher and administrator accounts.
- Activate or deactivate accounts.
- Manage examinations and review results.
- Create the initial administrator through a one-time development setup page or the CLI.

## 🧑‍🏫 Teacher Module

- Create and edit examinations with a title, subject, description, duration, marks, and optional availability dates.
- Manage exam publication state.
- Add and remove multiple-choice questions, assign marks, and set the correct answer.
- Review submitted student results.
- Manage only the teacher’s own exams; administrators can manage examinations across the portal.

## 🎓 Student Module

- Register with student details and sign in to the portal.
- Browse available examinations and review instructions before starting.
- Complete a timed exam with a visible countdown and answer progress.
- Have selected answers autosaved during an attempt.
- Submit an attempt, or have it automatically submitted when its deadline expires.
- Review submitted results.

## 🧰 Technologies Used

| Technology | Use |
|---|---|
| Python 3.10+ | Application language |
| Flask 3.x | Web application and command-line interface |
| Flask-SQLAlchemy / SQLAlchemy | ORM, models, and database access |
| SQLite | Default application database |
| PyMySQL | MySQL connectivity for the import utility |
| HTML5, CSS3, and vanilla JavaScript | Templates, responsive styling, and browser-side interactions |
| python-dotenv | Load local environment configuration |
| Werkzeug and bcrypt | Password hashing and legacy bcrypt password verification |
| pytest | Application tests |
| Waitress | Included WSGI server for Windows deployments |

## 🗂️ Project Structure

```text
Online-Examination-System/
├── app.py                    # Flask app, routes, authentication, and CLI commands
├── models.py                 # SQLAlchemy models
├── requirements.txt          # Python dependencies
├── .env.example              # Example local environment configuration
├── .gitignore
├── README.md
├── README.txt                # Existing project notes
├── database/
│   └── exam_system.sql       # Legacy MySQL schema reference
├── scripts/
│   └── import_mysql.py       # Copy compatible MySQL data into SQLite
├── instance/
│   └── .gitkeep              # Runtime database and secret-key location
├── static/
│   ├── css/
│   │   └── style.css         # Application styling
│   └── js/
│       └── app.js            # Browser interactions and exam timer/autosave
├── templates/                # Jinja templates for pages and role dashboards
└── tests/
    └── test_app.py           # Application tests
```

The SQLite database and generated development secret are runtime files under `instance/` and are excluded from Git.

## ⚙️ Installation and Setup

### Prerequisites

- Python 3.10 or later
- Git, if cloning the repository

### Windows PowerShell

1. Clone the repository and enter the project folder:

   ```powershell
   git clone https://github.com/Abhi210405/Online-Examination-System.git
   Set-Location Online-Examination-System
   ```

2. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use the virtual environment’s Python directly in the commands below (for example, `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`).

3. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Create a local environment file and set a private secret key:

   ```powershell
   Copy-Item .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Copy the generated value into `.env` as `SECRET_KEY`. Keep `.env` private and do not commit it. The example configuration uses development mode; production requires `APP_ENV=production` and a private secret key of at least 32 characters.

## ▶️ How to Run the Project

1. Initialize the database:

   ```powershell
   flask --app app init-db
   ```

2. Create the first administrator using the interactive CLI prompts:

   ```powershell
   flask --app app create-admin
   ```

   Alternatively, while running locally in development and before an administrator exists, open `http://127.0.0.1:5000/setup/first-admin` and complete the one-time setup form. The page is unavailable in production.

3. Start the Flask development server:

   ```powershell
   flask --app app run
   ```

4. Open [http://127.0.0.1:5000/](http://127.0.0.1:5000/) in your browser and sign in with the administrator account you created.

The development server is intended for local development. For a Windows WSGI deployment, Waitress is included:

```powershell
waitress-serve --host=127.0.0.1 app:app
```

For production, configure `APP_ENV=production`, a private `SECRET_KEY`, and a persistent `DATABASE_URL`; terminate TLS at a trusted reverse proxy.

## 🧪 Tests

Run the application test suite from the project root:

```powershell
python -m pytest
```

## 🗃️ Database Import

The optional importer copies an existing MySQL database into the configured SQLite database. It checks that the expected tables and columns match, stops if the destination tables are not empty, and does not modify the MySQL source. Back up your data before importing.

Set `SOURCE_DATABASE_URL` to a SQLAlchemy MySQL URL using the PyMySQL driver, then run the importer:

```powershell
$env:SOURCE_DATABASE_URL = "mysql+pymysql://<user>:<password>@<host>/<database>"
python scripts\import_mysql.py
```

The destination is selected with `DATABASE_URL`; if it is not set, the application uses `instance/college_exam_system.sqlite3`. The legacy MySQL schema reference is in `database/exam_system.sql`.

## 🚀 Future Enhancements

Potential future improvements—not currently part of the application—include:

- Additional question formats beyond multiple-choice questions.
- Downloadable result reports.
- More detailed exam and performance analytics.
- Email notifications for exam schedules and results.

## 👤 Author

**Abhinandan Pandey**

## 🔗 GitHub Repository

[Abhi210405/Online-Examination-System](https://github.com/Abhi210405/Online-Examination-System)
