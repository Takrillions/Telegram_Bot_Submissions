import asyncio
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from database import Database
from handlers import FeedbackRuntime, TopicCleaner, register_handlers
from scheduler import TenantScheduler


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_path: str
    default_timezone: str
    default_reset_days: int
    default_notice_text: str
    scheduler_check_seconds: int
    media_group_delay: float

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не задан в .env")

        reset_days = int(os.getenv("DEFAULT_RESET_DAYS", "30"))
        if reset_days < 2:
            raise RuntimeError("DEFAULT_RESET_DAYS должен быть не меньше 2")

        check_seconds = int(os.getenv("SCHEDULER_CHECK_SECONDS", "60"))
        if check_seconds < 10:
            raise RuntimeError(
                "SCHEDULER_CHECK_SECONDS должен быть не меньше 10"
            )

        media_group_delay = float(os.getenv("MEDIA_GROUP_DELAY", "0.8"))
        if media_group_delay < 0.2:
            raise RuntimeError("MEDIA_GROUP_DELAY должен быть не меньше 0.2")

        return cls(
            bot_token=token,
            database_path=os.getenv(
                "DATABASE_PATH", "feedback_bot.sqlite3"
            ).strip(),
            default_timezone=os.getenv(
                "DEFAULT_TIMEZONE", "Asia/Tashkent"
            ).strip(),
            default_reset_days=reset_days,
            default_notice_text=os.getenv(
                "DEFAULT_NOTICE_TEXT",
                "Через 24 часа история предложки будет автоматически очищена.",
            ).strip(),
            scheduler_check_seconds=check_seconds,
            media_group_delay=media_group_delay,
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    settings = Settings.from_env()

    db = Database(settings.database_path)
    await db.init()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    me = await bot.get_me()
    logging.info("Starting @%s (%s)", me.username, me.id)

    runtime = FeedbackRuntime(
        bot=bot,
        db=db,
        media_group_delay=settings.media_group_delay,
    )
    cleaner = TopicCleaner(bot=bot, db=db)

    dp = Dispatcher()
    register_handlers(
        dispatcher=dp,
        bot=bot,
        db=db,
        runtime=runtime,
        cleaner=cleaner,
        settings=settings,
    )

    scheduler = TenantScheduler(
        bot=bot,
        db=db,
        cleaner=cleaner,
        check_seconds=settings.scheduler_check_seconds,
    )

    scheduler.start()
    await scheduler.tick()

    try:
        # Этот проект использует long polling.
        # Если раньше для токена был установлен webhook, снимаем его.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        scheduler.shutdown()
        await runtime.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
