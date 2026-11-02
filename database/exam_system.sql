CREATE DATABASE IF NOT EXISTS college_exam_system CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE college_exam_system;

CREATE TABLE users (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 name VARCHAR(120) NOT NULL,
 email VARCHAR(190) NOT NULL UNIQUE,
 password VARCHAR(255) NOT NULL,
 role ENUM('admin','teacher','student') NOT NULL DEFAULT 'student',
 roll_number VARCHAR(50) NULL,
 department VARCHAR(100) NULL,
 status ENUM('active','inactive') NOT NULL DEFAULT 'active',
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE exams (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 title VARCHAR(180) NOT NULL,
 subject VARCHAR(120) NOT NULL,
 description TEXT NULL,
 duration_minutes INT UNSIGNED NOT NULL,
 total_marks DECIMAL(8,2) NOT NULL,
 start_at DATETIME NULL,
 end_at DATETIME NULL,
 created_by INT UNSIGNED NOT NULL,
 status ENUM('draft','published','closed') NOT NULL DEFAULT 'published',
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT fk_exam_creator FOREIGN KEY(created_by) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE questions (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 exam_id INT UNSIGNED NOT NULL,
 question_text TEXT NOT NULL,
 marks DECIMAL(6,2) NOT NULL DEFAULT 1,
 CONSTRAINT fk_question_exam FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE options (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 question_id INT UNSIGNED NOT NULL,
 option_text TEXT NOT NULL,
 is_correct TINYINT(1) NOT NULL DEFAULT 0,
 CONSTRAINT fk_option_question FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE attempts (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 exam_id INT UNSIGNED NOT NULL,
 student_id INT UNSIGNED NOT NULL,
 started_at DATETIME NOT NULL,
 deadline_at DATETIME NOT NULL,
 submitted_at DATETIME NULL,
 score DECIMAL(8,2) NOT NULL DEFAULT 0,
 UNIQUE KEY uq_exam_student(exam_id,student_id),
 CONSTRAINT fk_attempt_exam FOREIGN KEY(exam_id) REFERENCES exams(id),
 CONSTRAINT fk_attempt_student FOREIGN KEY(student_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE answers (
 id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
 attempt_id INT UNSIGNED NOT NULL,
 question_id INT UNSIGNED NOT NULL,
 selected_option_id INT UNSIGNED NULL,
 is_correct TINYINT(1) NOT NULL DEFAULT 0,
 UNIQUE KEY uq_attempt_question(attempt_id,question_id),
 CONSTRAINT fk_answer_attempt FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
 CONSTRAINT fk_answer_question FOREIGN KEY(question_id) REFERENCES questions(id),
 CONSTRAINT fk_answer_option FOREIGN KEY(selected_option_id) REFERENCES options(id)
) ENGINE=InnoDB;

-- Create the first admin account using the one-time setup page, then delete that page.
