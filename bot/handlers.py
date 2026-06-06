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
        if not _is_admin(message.from_user.id):
            return

        if message.chat.type in ("group", "supergroup"):
            await db.register_chat(message.chat.id, message.chat.title or "")

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
            "Просто напиши число — в любой чат с ботом или в личку.\n"
            "Одна попытка, менять нельзя. Удачи 🍀"
        )

        # Broadcast to all known group chats
        known_chats = await db.get_all_chats()
        for cid in known_chats:
            try:
                await message.bot.send_message(cid, announce, parse_mode="HTML")
            except Exception:
                pass

        # If started from private or chat not in list yet — reply directly
        if message.chat.id not in known_chats:
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
