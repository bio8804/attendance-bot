from __future__ import annotations

import logging
import os
import asyncio
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

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

AWAITING_FULL_NAME = 1


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


def is_admin(user_id: int) -> bool:
    return user_id in parse_admin_ids()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    user_id = employee_id(update)
    if is_admin(user_id):
        store.register_employee(user_id, employee_name(update), employee_username(update))
        await update.message.reply_text(
            "Вы зарегистрированы как администратор.\n\n"
            "Команды администратора:\n"
            "/requests - заявки на регистрацию\n"
            "/approve ID - одобрить сотрудника\n"
            "/reject ID - отклонить заявку\n"
            "/employees - список сотрудников",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    if store.is_approved_employee(user_id):
        await update.message.reply_text(
            "Вы уже зарегистрированы. Можно пользоваться ботом.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    if store.has_pending_request(user_id):
        await update.message.reply_text(
            "Ваша заявка уже отправлена администратору. Пожалуйста, дождитесь одобрения."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Здравствуйте. Для регистрации введите имя и фамилию одним сообщением.\n\n"
        "Например: Иван Иванов"
    )
    return AWAITING_FULL_NAME


async def receive_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    requested_name = (update.message.text or "").strip()
    if len(requested_name.split()) < 2:
        await update.message.reply_text("Пожалуйста, введите имя и фамилию. Например: Иван Иванов")
        return AWAITING_FULL_NAME

    user_id = employee_id(update)
    store.create_access_request(
        user_id=user_id,
        requested_name=requested_name,
        telegram_name=employee_name(update),
        username=employee_username(update),
    )

    admin_ids = parse_admin_ids()
    if not admin_ids:
        await update.message.reply_text(
            "Заявка сохранена, но администратор пока не настроен. "
            "Попросите владельца бота добавить ADMIN_IDS в настройках."
        )
        return ConversationHandler.END

    for admin_id in admin_ids:
        await context.bot.send_message(
            chat_id=admin_id,
            text=(
                "Новая заявка на регистрацию:\n"
                f"ID: {user_id}\n"
                f"Имя: {requested_name}\n"
                f"Telegram: {employee_name(update)}"
                f"{' (@' + employee_username(update) + ')' if employee_username(update) else ''}\n\n"
                f"Одобрить: /approve {user_id}\n"
                f"Отклонить: /reject {user_id}"
            ),
        )

    await update.message.reply_text(
        "Заявка отправлена администратору. После одобрения вы сможете отмечать приход и уход."
    )
    return ConversationHandler.END


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    await update.message.reply_text("Регистрация отменена. Чтобы начать заново, нажмите /start.")
    return ConversationHandler.END


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await update.message.reply_text(f"Ваш Telegram ID: {employee_id(update)}")


async def ensure_approved(update: Update) -> bool:
    if is_admin(employee_id(update)):
        store.register_employee(employee_id(update), employee_name(update), employee_username(update))
        return True

    if store.is_approved_employee(employee_id(update)):
        return True

    if store.has_pending_request(employee_id(update)):
        await update.message.reply_text("Ваша заявка еще ожидает одобрения администратора.")
        return False

    await update.message.reply_text("Сначала отправьте заявку на регистрацию через /start.")
    return False


async def check_in(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await ensure_approved(update):
        return

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
    if not await ensure_approved(update):
        return

    record = store.check_out(employee_id(update), employee_name(update), employee_username(update))

    await update.message.reply_text(
        f"Уход отмечен: {record.check_out}\n"
        f"Дата: {record.work_date}\n"
        f"Отработано: {format_minutes(record.worked_minutes)}",
        reply_markup=MAIN_KEYBOARD,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await ensure_approved(update):
        return

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


async def requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not is_admin(employee_id(update)):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    pending = store.pending_requests()
    if not pending:
        await update.message.reply_text("Новых заявок нет.")
        return

    lines = ["Заявки на регистрацию:"]
    for request in pending:
        username = f" (@{request['username']})" if request["username"] else ""
        lines.append(
            f"{request['user_id']} - {request['requested_name']}{username}\n"
            f"Одобрить: /approve {request['user_id']}\n"
            f"Отклонить: /reject {request['user_id']}"
        )
    await update.message.reply_text("\n\n".join(lines))


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(employee_id(update)):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /approve TELEGRAM_ID")
        return

    approved_user_id = int(context.args[0])
    request = store.approve_request(approved_user_id)
    if request is None:
        await update.message.reply_text("Активная заявка с таким ID не найдена.")
        return

    await update.message.reply_text(f"Сотрудник одобрен: {request['requested_name']}")
    await context.bot.send_message(
        chat_id=approved_user_id,
        text="Ваша заявка одобрена. Теперь можно отмечать приход и уход.",
        reply_markup=MAIN_KEYBOARD,
    )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(employee_id(update)):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /reject TELEGRAM_ID")
        return

    rejected_user_id = int(context.args[0])
    request = store.reject_request(rejected_user_id)
    if request is None:
        await update.message.reply_text("Активная заявка с таким ID не найдена.")
        return

    await update.message.reply_text(f"Заявка отклонена: {request['requested_name']}")
    await context.bot.send_message(
        chat_id=rejected_user_id,
        text="Ваша заявка на регистрацию отклонена. Для уточнения обратитесь к администратору.",
    )


async def employees(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not is_admin(employee_id(update)):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    rows = store.employees()
    if not rows:
        await update.message.reply_text("Одобренных сотрудников пока нет.")
        return

    lines = ["Сотрудники:"]
    for index, row in enumerate(rows, start=1):
        username = f" (@{row['username']})" if row["username"] else ""
        lines.append(f"{index}. {row['full_name']}{username} - {row['user_id']}")
    await update.message.reply_text("\n".join(lines))


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
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                AWAITING_FULL_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_full_name)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_registration)],
        )
    )
    application.add_handler(CommandHandler("in", check_in))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("out", check_out))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("today", today_report))
    application.add_handler(CommandHandler("inside", inside))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("export", export_report))
    application.add_handler(CommandHandler("requests", requests))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))
    application.add_handler(CommandHandler("employees", employees))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Attendance bot is running")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
