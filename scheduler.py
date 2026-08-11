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


class TenantScheduler:
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
            id="tenant_reset_tick",
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
            tenants = await self.db.list_enabled_tenants()
            now = utc_now()

            for tenant in tenants:
                try:
                    await self._process_tenant(tenant, now)
                except Exception:
                    logger.exception(
                        "Ошибка scheduler tenant=%s",
                        tenant["owner_id"],
                    )

    async def _process_tenant(self, tenant, now) -> None:
        owner_id = int(tenant["owner_id"])
        reset_at = dt_from_db(str(tenant["next_reset_at"]))
        cycle_key = dt_to_db(reset_at)

        # В последние 24 часа каждый tick ищет только тех подписчиков,
        # которые ещё не получили предупреждение этого цикла.
        # Это также покрывает новых пользователей, пришедших после начала
        # 24-часового окна.
        if reset_at - timedelta(hours=24) <= now < reset_at:
            await self._broadcast_missing_notices(
                tenant=tenant,
                cycle_key=cycle_key,
            )

        if now < reset_at:
            return

        logger.info(
            "Авто-сброс tenant=%s cutoff=%s",
            owner_id,
            cycle_key,
        )

        # Удаляем только темы, созданные ДО планового времени сброса.
        # Если scheduler сработал с небольшой задержкой и новая тема была
        # создана уже после reset_at, свежая тема не пострадает.
        result = await self.cleaner.cleanup_created_before(
            owner_id=owner_id,
            cutoff=reset_at,
        )

        if result["failed"] > 0:
            logger.error(
                "tenant=%s: %s тем не удалось удалить/закрыть; "
                "сброс будет повторён следующим tick",
                owner_id,
                result["failed"],
            )
            return

        interval_days = int(tenant["reset_interval_days"])
        next_reset = reset_at

        # Если процесс не работал несколько циклов, возвращаем расписание
        # в будущее, а не запускаем десятки мгновенных сбросов подряд.
        while next_reset <= now:
            next_reset += timedelta(days=interval_days)

        await self.db.advance_tenant_reset(
            owner_id=owner_id,
            next_reset_at=next_reset,
        )

        logger.info(
            "tenant=%s: авто-сброс завершён; next_reset=%s",
            owner_id,
            dt_to_db(next_reset),
        )

    async def _broadcast_missing_notices(
        self,
        *,
        tenant,
        cycle_key: str,
    ) -> None:
        owner_id = int(tenant["owner_id"])
        text = str(tenant["notice_text"]).strip()

        if not text:
            return

        user_ids = await self.db.get_unnotified_subscribers(
            owner_id=owner_id,
            cycle_at=cycle_key,
        )

        if not user_ids:
            return

        logger.info(
            "tenant=%s: предупреждение цикла %s, осталось %s пользователей",
            owner_id,
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
                        "tenant=%s: TelegramBadRequest notice user=%s",
                        owner_id,
                        user_id,
                    )
                    delivered_or_terminal = True
                except Exception:
                    logger.exception(
                        "tenant=%s: повторная отправка notice user=%s не удалась",
                        owner_id,
                        user_id,
                    )

            except TelegramForbiddenError:
                # Блокировка бота — терминальное состояние для текущего цикла.
                # Записываем как обработанное, чтобы не пытаться каждую минуту.
                await self.db.set_user_blocked(user_id, True)
                delivered_or_terminal = True

            except TelegramBadRequest:
                logger.warning(
                    "tenant=%s: TelegramBadRequest notice user=%s",
                    owner_id,
                    user_id,
                )
                delivered_or_terminal = True

            except Exception:
                # Сетевые/временные ошибки не помечаем как доставленные:
                # следующий scheduler tick попробует ещё раз.
                logger.exception(
                    "tenant=%s: временная ошибка notice user=%s",
                    owner_id,
                    user_id,
                )

            if delivered_or_terminal:
                await self.db.mark_notification_sent(
                    owner_id=owner_id,
                    cycle_at=cycle_key,
                    user_id=user_id,
                )

            # Консервативный темп массовой рассылки.
            await asyncio.sleep(0.05)
