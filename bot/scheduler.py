import asyncio
import logging
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from btc import get_btc_price

logger = logging.getLogger(__name__)

MSK = pytz.timezone("Europe/Moscow")


class Scheduler:
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self._scheduler = AsyncIOScheduler(timezone=MSK)

    def start(self):
        self._scheduler.start()

    def stop(self):
        self._scheduler.shutdown(wait=False)

    def schedule_round_end(self, round_id: int, run_at: datetime):
        """Schedule the job that resolves a round at the given UTC datetime."""
        job_id = f"round_{round_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            self._resolve_round,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            args=[round_id],
        )
        logger.info(f"Scheduled round {round_id} to resolve at {run_at} MSK")

    async def _resolve_round(self, round_id: int):
        logger.info(f"Resolving round {round_id}")
        round_ = await self.db.get_round_by_id(round_id)
        if not round_ or not round_["is_active"]:
            logger.info(f"Round {round_id} already closed or not found.")
            return

        chat_id = round_["chat_id"]
        target_time = round_["target_time"]

        try:
            actual_price = await get_btc_price()
        except Exception as e:
            logger.error(f"Failed to fetch BTC price: {e}")
            await self.bot.send_message(
                chat_id,
                "😬 Не удалось получить цену BTC — что-то пошло не так с биржей.\n"
                "Раунд закрыт без результатов.",
            )
            await self.db.deactivate_round(round_id)
            return

        guesses = await self.db.get_guesses(round_id)

        if not guesses:
            await self.db.deactivate_round(round_id)
            await self.bot.send_message(
                chat_id,
                f"⏰ Время подошло к концу — <b>{target_time} МСК</b>.\n"
                f"💰 Цена BTC: <b>${actual_price:,.2f}</b>\n\n"
                "Никто не рискнул сделать ставку 🦗",
                parse_mode="HTML",
            )
            return

        # Sort by closeness, then by submission time — earlier wins on tie
        sorted_guesses = sorted(
            guesses,
            key=lambda g: (abs(float(g["guess"]) - actual_price), g["created_at"]),
        )

        winner = sorted_guesses[0]
        diff = abs(float(winner["guess"]) - actual_price)
        display_name = f"@{winner['username']}" if winner["username"] else winner["first_name"] or "Неизвестный"

        await self.db.close_round(
            round_id,
            actual_price,
            winner["user_id"],
            winner["username"] or winner["first_name"],
            float(winner["guess"]),
        )

        # Build top-5 results table
        lines = []
        for i, g in enumerate(sorted_guesses[:5], 1):
            name = f"@{g['username']}" if g["username"] else g["first_name"] or "???"
            delta = abs(float(g["guess"]) - actual_price)
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
            lines.append(f"{medal} {name} — ${float(g['guess']):,.2f} (±${delta:,.2f})")
        results_text = "\n".join(lines)

        total = len(guesses)
        suffix = f"\n<i>...и ещё {total - 5} участников</i>" if total > 5 else ""

        await self.bot.send_message(
            chat_id,
            f"⏰ Время подошло к концу — <b>{target_time} МСК</b>.\n"
            f"💰 Реальная цена BTC: <b>${actual_price:,.2f}</b>\n\n"
            f"🏆 Победитель: {display_name}\n"
            f"    Ставка: <b>${float(winner['guess']):,.2f}</b> — промахнулся на ${diff:,.2f}\n\n"
            f"📊 Топ-5 ставок:\n{results_text}{suffix}",
            parse_mode="HTML",
        )

    async def reschedule_active_rounds(self):
        """Re-schedule jobs for active rounds after bot restart."""
        rounds = await self.db.get_all_active_rounds()
        now = datetime.now(MSK)
        for round_ in rounds:
            target_dt = round_["target_datetime"]
            if target_dt.tzinfo is None:
                target_dt = MSK.localize(target_dt)
            if target_dt > now:
                self.schedule_round_end(round_["id"], target_dt)
            else:
                asyncio.create_task(self._resolve_round(round_["id"]))
