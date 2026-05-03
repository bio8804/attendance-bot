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
    check_in_distance_m: int | None = None
    check_out_distance_m: int | None = None

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
                    status TEXT NOT NULL DEFAULT 'approved',
                    created_at TEXT NOT NULL
                )
                """
            )
            self.ensure_employee_status_column(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    work_date TEXT NOT NULL,
                    check_in TEXT,
                    check_out TEXT,
                    check_in_lat REAL,
                    check_in_lon REAL,
                    check_in_distance_m INTEGER,
                    check_out_lat REAL,
                    check_out_lon REAL,
                    check_out_distance_m INTEGER,
                    FOREIGN KEY (user_id) REFERENCES employees(user_id),
                    UNIQUE (user_id, work_date)
                )
                """
            )
            self.ensure_attendance_geo_columns(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS access_requests (
                    user_id INTEGER PRIMARY KEY,
                    requested_name TEXT NOT NULL,
                    telegram_name TEXT NOT NULL,
                    username TEXT,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def ensure_employee_status_column(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(employees)").fetchall()
        }
        if "status" not in columns:
            connection.execute(
                "ALTER TABLE employees ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
            )

    def ensure_attendance_geo_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(attendance)").fetchall()
        }
        geo_columns = {
            "check_in_lat": "REAL",
            "check_in_lon": "REAL",
            "check_in_distance_m": "INTEGER",
            "check_out_lat": "REAL",
            "check_out_lon": "REAL",
            "check_out_distance_m": "INTEGER",
        }
        for column_name, column_type in geo_columns.items():
            if column_name not in columns:
                connection.execute(f"ALTER TABLE attendance ADD COLUMN {column_name} {column_type}")

    def register_employee(self, user_id: int, full_name: str, username: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO employees (user_id, full_name, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = employees.full_name,
                    username = excluded.username,
                    status = 'approved'
                """,
                (user_id, full_name, username, utc_now()),
            )

    def is_approved_employee(self, user_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM employees WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None and row["status"] == "approved"

    def has_pending_request(self, user_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM access_requests WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchone()
        return row is not None

    def create_access_request(
        self,
        user_id: int,
        requested_name: str,
        telegram_name: str,
        username: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO access_requests (
                    user_id, requested_name, telegram_name, username, status, requested_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    requested_name = excluded.requested_name,
                    telegram_name = excluded.telegram_name,
                    username = excluded.username,
                    status = 'pending',
                    requested_at = excluded.requested_at,
                    reviewed_at = NULL
                """,
                (user_id, requested_name, telegram_name, username, utc_now()),
            )

    def pending_requests(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT user_id, requested_name, telegram_name, username, requested_at
                FROM access_requests
                WHERE status = 'pending'
                ORDER BY requested_at
                """
            ).fetchall()

    def approve_request(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            request = connection.execute(
                """
                SELECT user_id, requested_name, username
                FROM access_requests
                WHERE user_id = ? AND status = 'pending'
                """,
                (user_id,),
            ).fetchone()
            if request is None:
                return None

            connection.execute(
                """
                INSERT INTO employees (user_id, full_name, username, status, created_at)
                VALUES (?, ?, ?, 'approved', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username,
                    status = 'approved'
                """,
                (request["user_id"], request["requested_name"], request["username"], utc_now()),
            )
            connection.execute(
                """
                UPDATE access_requests
                SET status = 'approved', reviewed_at = ?
                WHERE user_id = ?
                """,
                (utc_now(), user_id),
            )
            return request

    def reject_request(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            request = connection.execute(
                """
                SELECT user_id, requested_name
                FROM access_requests
                WHERE user_id = ? AND status = 'pending'
                """,
                (user_id,),
            ).fetchone()
            if request is None:
                return None

            connection.execute(
                """
                UPDATE access_requests
                SET status = 'rejected', reviewed_at = ?
                WHERE user_id = ?
                """,
                (utc_now(), user_id),
            )
            return request

    def employees(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT user_id, full_name, username, created_at
                FROM employees
                WHERE status = 'approved'
                    AND user_id NOT IN (SELECT user_id FROM admins)
                ORDER BY full_name COLLATE NOCASE
                """
            ).fetchall()

    def add_admin(self, user_id: int, full_name: str, username: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admins (user_id, full_name, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username
                """,
                (user_id, full_name, username, utc_now()),
            )

    def remove_admin(self, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def is_db_admin(self, user_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM admins WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None

    def has_db_admins(self) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
        return row is not None

    def admins(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT user_id, full_name, username, created_at
                FROM admins
                ORDER BY full_name COLLATE NOCASE
                """
            ).fetchall()

    def check_in(
        self,
        user_id: int,
        full_name: str,
        username: str | None,
        latitude: float,
        longitude: float,
        distance_m: int,
    ) -> AttendanceRecord:
        self.register_employee(user_id, full_name, username)
        work_date = today()
        now_time = current_time()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO attendance (
                    user_id, work_date, check_in, check_in_lat, check_in_lon, check_in_distance_m
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, work_date) DO UPDATE SET
                    check_in = excluded.check_in,
                    check_out = NULL,
                    check_in_lat = excluded.check_in_lat,
                    check_in_lon = excluded.check_in_lon,
                    check_in_distance_m = excluded.check_in_distance_m,
                    check_out_lat = NULL,
                    check_out_lon = NULL,
                    check_out_distance_m = NULL
                """,
                (user_id, work_date, now_time, latitude, longitude, distance_m),
            )

        return self.get_record(user_id, work_date)

    def check_out(
        self,
        user_id: int,
        full_name: str,
        username: str | None,
        latitude: float,
        longitude: float,
        distance_m: int,
    ) -> AttendanceRecord:
        self.register_employee(user_id, full_name, username)
        work_date = today()
        now_time = current_time()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO attendance (
                    user_id, work_date, check_out, check_out_lat, check_out_lon, check_out_distance_m
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, work_date) DO UPDATE SET
                    check_out = excluded.check_out,
                    check_out_lat = excluded.check_out_lat,
                    check_out_lon = excluded.check_out_lon,
                    check_out_distance_m = excluded.check_out_distance_m
                """,
                (user_id, work_date, now_time, latitude, longitude, distance_m),
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
                    attendance.check_out,
                    attendance.check_in_distance_m,
                    attendance.check_out_distance_m
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
            check_in_distance_m=row["check_in_distance_m"],
            check_out_distance_m=row["check_out_distance_m"],
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
                    attendance.check_out,
                    attendance.check_in_distance_m,
                    attendance.check_out_distance_m
                FROM employees
                LEFT JOIN attendance
                    ON employees.user_id = attendance.user_id
                    AND attendance.work_date = ?
                WHERE employees.status = 'approved'
                    AND employees.user_id NOT IN (SELECT user_id FROM admins)
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
                check_in_distance_m=row["check_in_distance_m"],
                check_out_distance_m=row["check_out_distance_m"],
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
