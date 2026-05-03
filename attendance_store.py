from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"


@dataclass(frozen=True)
class AttendanceRecord:
    user_id: int
    full_name: str
    username: str | None
    work_date: str
    check_in: str | None
    check_out: str | None

    @property
    def is_inside(self) -> bool:
        return self.check_in is not None and self.check_out is None

    @property
    def worked_minutes(self) -> int | None:
        if not self.check_in or not self.check_out:
            return None

        start = parse_time(self.check_in)
        end = parse_time(self.check_out)
        if end < start:
            return None

        return int((end - start).total_seconds() // 60)


class AttendanceStore:
    def __init__(self, db_path: str | Path = "attendance.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    work_date TEXT NOT NULL,
                    check_in TEXT,
                    check_out TEXT,
                    FOREIGN KEY (user_id) REFERENCES employees(user_id),
                    UNIQUE (user_id, work_date)
                )
                """
            )

    def register_employee(self, user_id: int, full_name: str, username: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO employees (user_id, full_name, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username
                """,
                (user_id, full_name, username, utc_now()),
            )

    def check_in(self, user_id: int, full_name: str, username: str | None) -> AttendanceRecord:
        self.register_employee(user_id, full_name, username)
        work_date = today()
        now_time = current_time()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO attendance (user_id, work_date, check_in)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, work_date) DO UPDATE SET
                    check_in = excluded.check_in,
                    check_out = NULL
                """,
                (user_id, work_date, now_time),
            )

        return self.get_record(user_id, work_date)

    def check_out(self, user_id: int, full_name: str, username: str | None) -> AttendanceRecord:
        self.register_employee(user_id, full_name, username)
        work_date = today()
        now_time = current_time()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO attendance (user_id, work_date, check_out)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, work_date) DO UPDATE SET
                    check_out = excluded.check_out
                """,
                (user_id, work_date, now_time),
            )

        return self.get_record(user_id, work_date)

    def get_record(self, user_id: int, work_date: str | None = None) -> AttendanceRecord:
        requested_date = work_date or today()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    employees.user_id,
                    employees.full_name,
                    employees.username,
                    attendance.work_date,
                    attendance.check_in,
                    attendance.check_out
                FROM employees
                LEFT JOIN attendance
                    ON employees.user_id = attendance.user_id
                    AND attendance.work_date = ?
                WHERE employees.user_id = ?
                """,
                (requested_date, user_id),
            ).fetchone()

        if row is None:
            raise ValueError("Employee is not registered")

        return AttendanceRecord(
            user_id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
            work_date=row["work_date"] or requested_date,
            check_in=row["check_in"],
            check_out=row["check_out"],
        )

    def daily_report(self, work_date: str | None = None) -> list[AttendanceRecord]:
        requested_date = work_date or today()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    employees.user_id,
                    employees.full_name,
                    employees.username,
                    ? AS work_date,
                    attendance.check_in,
                    attendance.check_out
                FROM employees
                LEFT JOIN attendance
                    ON employees.user_id = attendance.user_id
                    AND attendance.work_date = ?
                ORDER BY employees.full_name COLLATE NOCASE
                """,
                (requested_date, requested_date),
            ).fetchall()

        return [
            AttendanceRecord(
                user_id=row["user_id"],
                full_name=row["full_name"],
                username=row["username"],
                work_date=row["work_date"],
                check_in=row["check_in"],
                check_out=row["check_out"],
            )
            for row in rows
        ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return local_now().strftime(DATE_FORMAT)


def current_time() -> str:
    return local_now().strftime(TIME_FORMAT)


def local_now() -> datetime:
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Tashkent")
    return datetime.now(ZoneInfo(timezone_name))


def parse_time(value: str) -> datetime:
    parsed_time = datetime.strptime(value, TIME_FORMAT).time()
    return datetime.combine(date.today(), parsed_time)


def format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "-"

    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours} ч {remainder} мин"
    if hours:
        return f"{hours} ч"
    return f"{remainder} мин"


def is_valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        return False
    return True
