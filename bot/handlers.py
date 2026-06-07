import logging
import os
import re
from datetime import datetime, timedelta

import pytz
from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

MSK = pytz.timezone("Europe/Moscow")

# Admins list from ADMINS env var (comma‑separated), first one is main owner.
def _load_admins() -> tuple[set[int], int]:
    raw = os.environ.get("ADMINS", "").strip()
    if not raw:
        raise ValueError("ADMINS environment variable is required (comma‑separated Telegram user IDs)")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        raise ValueError("ADMINS must contain at least one numeric ID")
    return set(ids), ids[0]  # all admins, main owner

ADMIN_IDS, OWNER_ID = _load_admins()

# Дедлайны приёма ставок по раундам (только в памяти процесса, без БД).
# {round_id: datetime(MSK)} — после этого времени ставки не принимаются,
# но итоги всё равно подводятся в target_time раунда.
_reg_deadlines: dict[int, datetime] = {}


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _parse_price(text: str) -> float | None:
    """
    Try to parse a price from user input. Handles:
    - Decimal separator: both . and ,  (e.g. 95000.5 / 95000,5)
    - Thousands separator: spaces, commas, dots  (e.g. 95 000 / 95,000 / 95.000)
    - Mixed: 95.000,50 or 95,000.50
    - Trailing/leading spaces, $ sign
    """
    clean = text.strip().lstrip("$").strip()
    clean = clean.replace(" ", "")

    dot_pos = clean.rfind(".")
    comma_pos = clean.rfind(",")

    if dot_pos > comma_pos:
        clean = clean.replace(",", "")
    elif comma_pos > dot_pos:
        clean = clean.replace(".", "").replace(",", ".")
    else:
        clean = clean.replace(",", "")

    try:
        value = float(clean)
        if value > 0:
            return value
    except ValueError:
        pass
    return None


def _parse_time(time_str: str) -> tuple[int, int] | None:
    """Parse HH:MM string. Returns (hour, minute) or None."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", time_str.strip())
    if not match:
        return None
    h, m = int(match.group(1)), int(match.group(2))
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _build_target_datetime(hour: int, minute: int) -> datetime:
    """Build next occurrence of HH:MM in Moscow time."""
    now = datetime.now(MSK)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def register_handlers(dp: Dispatcher, db, scheduler):

    # ── /start HH:MM ──────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        if message.chat.type in ("group", "supergroup"):
            await db.register_chat(message.chat.id, message.chat.title or "")

        # Обычный пользователь в личке — приветствие
        if not _is_admin(message.from_user.id):
            if message.chat.type == "private":
                active = await db.get_global_active_round()
                if active:
                    await message.reply(
                        f"Привет! 🎯 Сейчас идёт раунд ставок до <b>{active['target_time']} МСК</b>.\n\n"
                        "Отправь мне цену BTC, которую прогнозируешь на это время.\n"
                        "Например: <code>98500</code>",
                        parse_mode="HTML",
                    )
                else:
                    await message.reply(
                        "Привет! 👋 Сейчас нет активного раунда.\n"
                        "Как только ведущий объявит о начале игры — просто отправь сюда своё число."
                    )
            return

        # Админ — запускаем раунд
        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await message.reply("Укажи время: /start HH:MM\nПример: /start 16:00")
            return

        parsed = _parse_time(args[1])
        if not parsed:
            await message.reply("Неверный формат времени. Пример: /start 16:00")
            return

        hour, minute = parsed

        existing = await db.get_global_active_round()
        if existing:
            await message.reply(
                f"⚠️ Глобальный раунд уже идёт — до {existing['target_time']} МСК.\n"
                "Дождись завершения или используй /cancel."
            )
            return

        target_dt = _build_target_datetime(hour, minute)
        time_str = f"{hour:02d}:{minute:02d}"

        # Create global round (chat_id = NULL)
        round_id = await db.create_round(time_str, target_dt, chat_id=message.chat.id)
        scheduler.schedule_round_end(round_id, target_dt)

        announce = (
            f"🎯 Ставки открыты! Угадай цену BTC ровно в <b>{time_str} МСК</b>.\n\n"
            "Напиши своё число в личку боту — одна попытка, менять нельзя. Удачи 🍀"
        )

        # Broadcast to all known group chats in batches
        known_chats = await db.get_all_chats()
        from scheduler import _send_batch
        await _send_batch(message.bot, known_chats, announce)

        # If started from private or a chat not yet in known_chats — reply directly
        if message.chat.id not in set(known_chats):
            await message.reply(announce, parse_mode="HTML")

    # ── /cancel ────────────────────────────────────────────────────────────────

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        if not _is_admin(message.from_user.id):
            return

        active = await db.get_global_active_round()
        if not active:
            await message.reply("Нет активного раунда.")
            return

        job_id = f"round_{active['id']}"
        try:
            scheduler._scheduler.remove_job(job_id)
        except Exception:
            pass

        await db.deactivate_round(active["id"])
        await message.reply("❌ Глобальный раунд отменён. Ставки аннулированы.")

    # ── /end ───────────────────────────────────────────────────────────────────

    @dp.message(Command("end"))
    async def cmd_end(message: Message):
        if not _is_admin(message.from_user.id):
            return

        active = await db.get_global_active_round()
        if not active:
            await message.reply("Нет активного раунда.")
            return

        job_id = f"round_{active['id']}"
        try:
            scheduler._scheduler.remove_job(job_id)
        except Exception:
            pass

        await message.reply("⏱ Завершаю глобальный раунд досрочно, иду за ценой BTC...")
        await scheduler._resolve_round(active["id"])

    # ── /end_reg HH:MM ──────────────────────────────────────────────────────────

    @dp.message(Command("end_reg"))
    async def cmd_end_reg(message: Message):
        if not _is_admin(message.from_user.id):
            return

        active = await db.get_global_active_round()
        if not active:
            await message.reply("Нет активного раунда.")
            return

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await message.reply("Укажи время окончания приёма ставок: /end_reg HH:MM\nПример: /end_reg 19:00")
            return

        parsed = _parse_time(args[1])
        if not parsed:
            await message.reply("Неверный формат времени. Пример: /end_reg 19:00")
            return

        hour, minute = parsed
        deadline = _build_target_datetime(hour, minute)
        # Дедлайн приёма не может быть позже подведения итогов.
        target_dt = MSK.localize(active["target_datetime"]) if active["target_datetime"].tzinfo is None else active["target_datetime"]
        if deadline > target_dt:
            await message.reply(
                f"⚠️ Приём ставок ({hour:02d}:{minute:02d}) не может быть позже подведения итогов "
                f"({active['target_time']} МСК)."
            )
            return

        _reg_deadlines[active["id"]] = deadline
        await message.reply(
            f"✅ Приём ставок закрывается в <b>{hour:02d}:{minute:02d} МСК</b>.\n"
            f"Итоги по-прежнему в <b>{active['target_time']} МСК</b>.",
            parse_mode="HTML",
        )

    # ── /status ────────────────────────────────────────────────────────────────

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        active = await db.get_global_active_round()
        if not active:
            await message.reply("Сейчас глобального раунда нет. Ждём следующего 👀")
            return

        guesses = await db.get_guesses(active["id"])
        count = len(guesses)
        await message.reply(
            f"⏳ Глобальный раунд идёт до <b>{active['target_time']} МСК</b>.\n"
            f"Уже поставили: {count} чел.",
            parse_mode="HTML",
        )

    # ── Обработка ставок (только личка) ──────────────────────────────────────

    @dp.message(F.text)
    async def handle_guess(message: Message):
        user = message.from_user
        text = message.text or ""

        if text.startswith("/"):
            return

        # Ставки принимаются только в личке
        if message.chat.type != "private":
            return

        price = _parse_price(text)
        if price is None:
            return

        active = await db.get_global_active_round()
        if not active:
            await message.reply(
                "Сейчас нет активного раунда 🤷\n"
                "Когда админ запустит игру, просто отправь сюда число."
            )
            return

        # Приём ставок закрыт, но итоги ещё не подведены
        deadline = _reg_deadlines.get(active["id"])
        if deadline is not None and datetime.now(MSK) >= deadline:
            await message.reply(
                f"⏰ Приём ставок уже закрыт ({deadline.strftime('%H:%M')} МСК).\n"
                f"Итоги розыгрыша будут в <b>{active['target_time']} МСК</b>.",
                parse_mode="HTML",
            )
            return

        success = await db.add_guess(
            round_id=active["id"],
            user_id=user.id,
            username=user.username,
            first_name=user.first_name or "",
            guess=price,
        )

        if success:
            display = f"@{user.username}" if user.username else user.first_name or "Участник"
            await message.reply(
                f"✅ {display}, принято! Твоя ставка: <b>${price:,.2f}</b> 🤞\n"
                f"Итоги розыгрыша в <b>{active['target_time']} МСК</b>.",
                parse_mode="HTML",
            )
            try:
                await message.react([{"type": "emoji", "emoji": "👍"}])
            except Exception:
                pass
        else:
            await message.reply("🚫 Ставка уже есть — изменить не получится.")
            try:
                await message.react([{"type": "emoji", "emoji": "👎"}])
            except Exception:
                pass
