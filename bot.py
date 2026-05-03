from __future__ import annotations

import logging
import os
import asyncio
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from attendance_store import AttendanceRecord, AttendanceStore, format_minutes, is_valid_date, today
from health_server import start_health_server
from report_exporter import export_daily_report


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()

default_db_path = Path(tempfile.gettempdir()) / "attendance.db"
store = AttendanceStore(os.getenv("ATTENDANCE_DB_PATH", str(default_db_path)))

BUTTON_IN = "Пришел"
BUTTON_OUT = "Ушел"
BUTTON_STATUS = "Мой статус"
BUTTON_TODAY = "Отчет за сегодня"
BUTTON_INSIDE = "Кто на работе"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BUTTON_IN, BUTTON_OUT],
        [BUTTON_STATUS, BUTTON_TODAY],
        [BUTTON_INSIDE],
    ],
    resize_keyboard=True,
)


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
        "Можно пользоваться кнопками ниже или командами:\n"
        "/in - отметить приход\n"
        "/out - отметить уход\n"
        "/status - текущий статус\n"
        "/today - отчет за сегодня\n"
        "/inside - кто сейчас на работе\n"
        "/export - Excel-отчет за сегодня\n"
        "/report YYYY-MM-DD - отчет за дату",
        reply_markup=MAIN_KEYBOARD,
    )


async def check_in(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    record = store.check_in(employee_id(update), employee_name(update), employee_username(update))

    if record.check_in:
        await update.message.reply_text(
            f"Приход отмечен: {record.check_in}\nДата: {record.work_date}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await update.message.reply_text("Не удалось отметить приход.", reply_markup=MAIN_KEYBOARD)


async def check_out(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    record = store.check_out(employee_id(update), employee_name(update), employee_username(update))

    await update.message.reply_text(
        f"Уход отмечен: {record.check_out}\n"
        f"Дата: {record.work_date}\n"
        f"Отработано: {format_minutes(record.worked_minutes)}",
        reply_markup=MAIN_KEYBOARD,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user_id = employee_id(update)
    store.register_employee(user_id, employee_name(update), employee_username(update))
    record = store.get_record(user_id)

    await update.message.reply_text(format_record_status(record), reply_markup=MAIN_KEYBOARD)


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
    await update.message.reply_text(format_report(report_date, records), reply_markup=MAIN_KEYBOARD)


async def today_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not can_view_report(employee_id(update)):
        await update.message.reply_text("У вас нет доступа к отчетам.", reply_markup=MAIN_KEYBOARD)
        return

    await update.message.reply_text(format_report(today(), store.daily_report()), reply_markup=MAIN_KEYBOARD)


async def inside(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not can_view_report(employee_id(update)):
        await update.message.reply_text("У вас нет доступа к отчетам.", reply_markup=MAIN_KEYBOARD)
        return

    records = [record for record in store.daily_report() if record.is_inside]
    await update.message.reply_text(format_inside(records), reply_markup=MAIN_KEYBOARD)


async def export_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = employee_id(update)
    if not can_view_report(user_id):
        await update.message.reply_text("У вас нет доступа к отчетам.", reply_markup=MAIN_KEYBOARD)
        return

    report_date = today()
    if context.args:
        report_date = context.args[0]
        if not is_valid_date(report_date):
            await update.message.reply_text("Дата должна быть в формате YYYY-MM-DD, например 2026-05-03.")
            return

    records = store.daily_report(report_date)
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = Path(temp_dir) / f"attendance_{report_date}.xlsx"
        export_daily_report(file_path, report_date, records)
        with file_path.open("rb") as document:
            await update.message.reply_document(
                document=document,
                filename=file_path.name,
                caption=f"Excel-отчет за {report_date}",
                reply_markup=MAIN_KEYBOARD,
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == BUTTON_IN:
        await check_in(update, context)
        return
    if text == BUTTON_OUT:
        await check_out(update, context)
        return
    if text == BUTTON_STATUS:
        await status(update, context)
        return
    if text == BUTTON_TODAY:
        await today_report(update, context)
        return
    if text == BUTTON_INSIDE:
        await inside(update, context)
        return

    await update.message.reply_text(
        "Я понимаю кнопки и команды: /in, /out, /status, /today, /inside, /report, /export.",
        reply_markup=MAIN_KEYBOARD,
    )


def format_record_status(record: AttendanceRecord) -> str:
    check_in = record.check_in or "не отмечен"
    check_out = record.check_out or "не отмечен"
    worked_time = format_minutes(record.worked_minutes)
    current_status = "на работе" if record.is_inside else "не на работе"
    return (
        f"Статус за {record.work_date}\n"
        f"Приход: {check_in}\n"
        f"Уход: {check_out}\n"
        f"Отработано: {worked_time}\n"
        f"Сейчас: {current_status}"
    )


def format_report(report_date: str, records: list[AttendanceRecord]) -> str:
    if not records:
        return f"Отчет за {report_date}\nСотрудников пока нет."

    completed_minutes = sum(record.worked_minutes or 0 for record in records)
    inside_count = sum(1 for record in records if record.is_inside)
    absent_count = sum(1 for record in records if not record.check_in)

    lines = [
        f"Отчет за {report_date}",
        f"Сотрудников: {len(records)}",
        f"На работе сейчас: {inside_count}",
        f"Без отметки прихода: {absent_count}",
        f"Итого закрытых часов: {format_minutes(completed_minutes)}",
        "",
    ]
    for index, record in enumerate(records, start=1):
        check_in = record.check_in or "-"
        check_out = record.check_out or "-"
        worked_time = format_minutes(record.worked_minutes)
        state = "на работе" if record.is_inside else "закрыто" if record.check_out else "нет прихода"
        username = f" (@{record.username})" if record.username else ""
        lines.append(
            f"{index}. {record.full_name}{username}: {check_in} - {check_out}, "
            f"{worked_time}, {state}"
        )

    return "\n".join(lines)


def format_inside(records: list[AttendanceRecord]) -> str:
    if not records:
        return "Сейчас никто не отмечен как находящийся на работе."

    lines = ["Сейчас на работе:"]
    for index, record in enumerate(records, start=1):
        username = f" (@{record.username})" if record.username else ""
        lines.append(f"{index}. {record.full_name}{username}, с {record.check_in}")
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
    application.add_handler(CommandHandler("today", today_report))
    application.add_handler(CommandHandler("inside", inside))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("export", export_report))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Attendance bot is running")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
