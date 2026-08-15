import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database, dt_from_db, dt_to_db, utc_now
from handlers import TopicCleaner

logger = logging.getLogger(__name__)


class ChannelScheduler:
    """
    APScheduler запускает один асинхронный tick.
    Сами даты циклов хранятся в SQLite, поэтому перезапуск процесса
    не сбрасывает 30-дневный отсчёт.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
        cleaner: TopicCleaner,
        check_seconds: int,
    ) -> None:
        self.bot = bot
        self.db = db
        self.cleaner = cleaner
        self.check_seconds = check_seconds

        self.scheduler = AsyncIOScheduler()
        self._tick_lock = asyncio.Lock()

    def start(self) -> None:
        self.scheduler.add_job(
            self.tick,
            trigger="interval",
            seconds=self.check_seconds,
            id="channel_reset_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def tick(self) -> None:
        if self._tick_lock.locked():
            return

        async with self._tick_lock:
            channels = await self.db.list_enabled_channels()
            now = utc_now()

            for channel in channels:
                try:
                    await self._process_channel(channel, now)
                except Exception:
                    logger.exception(
                        "Ошибка scheduler channel=%s",
                        channel["channel_id"],
                    )

    async def _process_channel(self, channel, now) -> None:
        channel_id = int(channel["channel_id"])
        if not bool(channel["auto_cleanup_enabled"]):
            logger.debug("Automatic cleanup disabled for channel=%s", channel_id)
            return
        reset_at = dt_from_db(str(channel["next_reset_at"]))
        cycle_key = dt_to_db(reset_at)

        # В последние 24 часа каждый tick ищет только тех подписчиков,
        # которые ещё не получили предупреждение этого цикла.
        # Это также покрывает новых пользователей, пришедших после начала
        # 24-часового окна.
        if reset_at - timedelta(hours=24) <= now < reset_at:
            await self._broadcast_missing_notices(
                channel=channel,
                cycle_key=cycle_key,
            )

        if now < reset_at:
            return

        logger.info(
            "Авто-сброс channel=%s cutoff=%s",
            channel_id,
            cycle_key,
        )

        # Удаляем только темы, созданные ДО планового времени сброса.
        # Если scheduler сработал с небольшой задержкой и новая тема была
        # создана уже после reset_at, свежая тема не пострадает.
        result = await self.cleaner.cleanup_by_policy(
            channel=channel,
            cutoff=reset_at,
            now=now,
        )

        if result["failed"] > 0:
            logger.error(
                "channel=%s: %s тем не удалось удалить/закрыть; "
                "сброс будет повторён следующим tick",
                channel_id,
                result["failed"],
            )
            return

        interval_days = int(channel["reset_interval_days"])
        next_reset = reset_at

        # Если процесс не работал несколько циклов, возвращаем расписание
        # в будущее, а не запускаем десятки мгновенных сбросов подряд.
        while next_reset <= now:
            next_reset += timedelta(days=interval_days)

        await self.db.advance_channel_reset(
            channel_id=channel_id,
            next_reset_at=next_reset,
        )

        logger.info(
            "channel=%s: авто-сброс завершён; next_reset=%s",
            channel_id,
            dt_to_db(next_reset),
        )

    async def _broadcast_missing_notices(
        self,
        *,
        channel,
        cycle_key: str,
    ) -> None:
        channel_id = int(channel["channel_id"])
        text = str(channel["notice_text"]).strip()

        if not text:
            return

        user_ids = await self.db.get_unnotified_subscribers(
            channel_id=channel_id,
            cycle_at=cycle_key,
        )

        if not user_ids:
            return

        logger.info(
            "channel=%s: предупреждение цикла %s, осталось %s пользователей",
            channel_id,
            cycle_key,
            len(user_ids),
        )

        for user_id in user_ids:
            delivered_or_terminal = False

            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=None,
                )
                await self.db.set_user_blocked(user_id, False)
                delivered_or_terminal = True

            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1.0)

                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode=None,
                    )
                    await self.db.set_user_blocked(
                        user_id,
                        False,
                    )
                    delivered_or_terminal = True
                except TelegramForbiddenError:
                    await self.db.set_user_blocked(
                        user_id,
                        True,
                    )
                    delivered_or_terminal = True
                except TelegramBadRequest:
                    logger.warning(
                        "channel=%s: TelegramBadRequest notice user=%s",
                        channel_id,
                        user_id,
                    )
                    delivered_or_terminal = True
                except Exception:
                    logger.exception(
                        "channel=%s: повторная отправка notice user=%s не удалась",
                        channel_id,
                        user_id,
                    )

            except TelegramForbiddenError:
                # Блокировка бота — терминальное состояние для текущего цикла.
                # Записываем как обработанное, чтобы не пытаться каждую минуту.
                await self.db.set_user_blocked(user_id, True)
                delivered_or_terminal = True

            except TelegramBadRequest:
                logger.warning(
                    "channel=%s: TelegramBadRequest notice user=%s",
                    channel_id,
                    user_id,
                )
                delivered_or_terminal = True

            except Exception:
                # Сетевые/временные ошибки не помечаем как доставленные:
                # следующий scheduler tick попробует ещё раз.
                logger.exception(
                    "channel=%s: временная ошибка notice user=%s",
                    channel_id,
                    user_id,
                )

            if delivered_or_terminal:
                await self.db.mark_notification_sent(
                    channel_id=channel_id,
                    cycle_at=cycle_key,
                    user_id=user_id,
                )

            # Консервативный темп массовой рассылки.
            await asyncio.sleep(0.05)
