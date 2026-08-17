import argparse
import asyncio
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from authorization import parse_superadmin_telegram_id
from command_menu import sync_command_menus
from database import Database
from release_runtime import clear_readiness, write_readiness
from handlers import FeedbackRuntime, TopicCleaner, register_handlers
from scheduler import ChannelScheduler


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_path: str
    default_timezone: str
    default_reset_days: int
    default_notice_text: str
    scheduler_check_seconds: int
    media_group_delay: float
    database_backup_dir: str
    database_backup_keep: int
    readiness_path: str
    release_id: str
    superadmin_telegram_id: int | None

    @classmethod
    def from_env(cls) -> "Settings":
        env_file = os.getenv("ENV_FILE", "").strip()
        load_dotenv(env_file or None)

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

        backup_keep = int(os.getenv("DATABASE_BACKUP_KEEP", "7"))
        if backup_keep < 1:
            raise RuntimeError("DATABASE_BACKUP_KEEP must be at least 1")

        superadmin_telegram_id = parse_superadmin_telegram_id(
            os.getenv("SUPERADMIN_TELEGRAM_ID")
        )

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
            database_backup_dir=os.getenv(
                "DATABASE_BACKUP_DIR", "backups"
            ).strip(),
            database_backup_keep=backup_keep,
            readiness_path=os.getenv("READINESS_PATH", "").strip(),
            release_id=os.getenv("RELEASE_ID", "local").strip() or "local",
            superadmin_telegram_id=superadmin_telegram_id,
        )


async def _create_database(settings: Settings) -> Database:
    return Database(
        settings.database_path,
        backup_dir=settings.database_backup_dir,
        backup_keep=settings.database_backup_keep,
    )


async def validate_release() -> None:
    settings = Settings.from_env()
    db = await _create_database(settings)
    await db.inspect_pending_migrations()
    bot = Bot(token=settings.bot_token)
    try:
        await bot.get_me()
    finally:
        await bot.session.close()


async def migrate_only() -> tuple[int, ...]:
    settings = Settings.from_env()
    db = await _create_database(settings)
    try:
        await db.init()
        return db.applied_migration_versions
    finally:
        await db.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    settings = Settings.from_env()
    if settings.superadmin_telegram_id is None:
        logging.getLogger(__name__).warning(
            "SUPERADMIN_TELEGRAM_ID is not configured; global bot-profile and Standard Custom Pack editing are disabled"
        )
    clear_readiness(settings.readiness_path)
    db = await _create_database(settings)
    await db.init()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await sync_command_menus(
        bot=bot,
        db=db,
        superadmin_telegram_id=settings.superadmin_telegram_id,
    )
    runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=settings.media_group_delay)
    cleaner = TopicCleaner(bot=bot, db=db)
    dp = Dispatcher()
    register_handlers(dispatcher=dp, bot=bot, db=db, runtime=runtime, cleaner=cleaner, settings=settings)
    scheduler = ChannelScheduler(bot=bot, db=db, cleaner=cleaner, check_seconds=settings.scheduler_check_seconds)
    scheduler.start()
    await scheduler.tick()

    async def mark_ready(*_) -> None:
        if settings.readiness_path:
            write_readiness(settings.readiness_path, release_id=settings.release_id, bot_id=me.id, bot_username=me.username, scheduler_ready=True, polling_ready=True)

    dp.startup.register(mark_ready)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        clear_readiness(settings.readiness_path)
        scheduler.shutdown()
        await runtime.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-release", action="store_true")
    parser.add_argument("--migrate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_release and args.migrate_only:
        parser.error("choose one special mode")
    if args.validate_release:
        asyncio.run(validate_release())
    elif args.migrate_only:
        applied = asyncio.run(migrate_only())
        print("MIGRATIONS_APPLIED=" + ",".join(map(str, applied)))
    else:
        asyncio.run(main())
