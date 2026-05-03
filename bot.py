from __future__ import annotations

import logging
import os
import asyncio

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from attendance_store import AttendanceRecord, AttendanceStore, is_valid_date, today
from health_server import start_health_server


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()

store = AttendanceStore(os.getenv("ATTENDANCE_DB_PATH", "/tmp/attendance.db"))


def employee_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Unknown"
    return user.full_name or user.username or str(user.id)


def employee_username(update: Update) -> str | None:
    user = update.effective_user
    return user.username if user else None


def employee_id(update: Update) -> int:
    user = update.effective_user
    if user is None:
        raise ValueError("Telegram user is missing")
    return user.id


def parse_admin_ids() -> set[int]:
    raw_ids = os.getenv("ADMIN_IDS", "").strip()
    if not raw_ids:
        return set()

    result: set[int] = set()
    for raw_id in raw_ids.split(","):
        raw_id = raw_id.strip()
        if raw_id:
            result.add(int(raw_id))
    return result


def can_view_report(user_id: int) -> bool:
    admin_ids = parse_admin_ids()
    return not admin_ids or user_id in admin_ids


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    store.register_employee(employee_id(update), employee_name(update), employee_username(update))
    await update.message.reply_text(
        "Готово, вы зарегистрированы.\n\n"
        "Команды:\n"
        "/in - отметить приход\n"
        "/out - отметить уход\n"
        "/status - текущий статус\n"
        "/report - отчет за сегодня"
    )


async def check_in(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    record = store.check_in(employee_id(update), employee_name(update), employee_username(update))

    if record.check_in:
        await update.message.reply_text(
            f"Приход отмечен: {record.check_in}\nДата: {record.work_date}"
        )
        return

    await update.message.reply_text("Не удалось отметить приход.")


async def check_out(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    record = store.check_out(employee_id(update), employee_name(update), employee_username(update))

    await update.message.reply_text(
        f"Уход отмечен: {record.check_out}\nДата: {record.work_date}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user_id = employee_id(update)
    store.register_employee(user_id, employee_name(update), employee_username(update))
    record = store.get_record(user_id)

    await update.message.reply_text(format_record_status(record))


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = employee_id(update)
    if not can_view_report(user_id):
        await update.message.reply_text("У вас нет доступа к отчетам.")
        return

    report_date = today()
    if context.args:
        report_date = context.args[0]
        if not is_valid_date(report_date):
            await update.message.reply_text("Дата должна быть в формате YYYY-MM-DD, например 2026-05-03.")
            return

    records = store.daily_report(report_date)
    await update.message.reply_text(format_report(report_date, records))


def format_record_status(record: AttendanceRecord) -> str:
    check_in = record.check_in or "не отмечен"
    check_out = record.check_out or "не отмечен"
    return (
        f"Статус за {record.work_date}\n"
        f"Приход: {check_in}\n"
        f"Уход: {check_out}"
    )


def format_report(report_date: str, records: list[AttendanceRecord]) -> str:
    if not records:
        return f"Отчет за {report_date}\nСотрудников пока нет."

    lines = [f"Отчет за {report_date}"]
    for index, record in enumerate(records, start=1):
        check_in = record.check_in or "-"
        check_out = record.check_out or "-"
        username = f" (@{record.username})" if record.username else ""
        lines.append(f"{index}. {record.full_name}{username}: {check_in} - {check_out}")

    return "\n".join(lines)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")

    asyncio.set_event_loop(asyncio.new_event_loop())
    start_health_server()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("in", check_in))
    application.add_handler(CommandHandler("out", check_out))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("report", report))

    logger.info("Attendance bot is running")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
