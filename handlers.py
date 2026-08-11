import asyncio
import html
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Hashable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from aiogram.utils.deep_linking import create_start_link

from database import Database, dt_from_db, utc_now

logger = logging.getLogger(__name__)


ADMIN_COMMANDS = {
    "setup",
    "panel",
    "set_period",
    "set_announcement",
    "set_timezone",
}


def topic_name(user: User) -> str:
    full_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    username = f"@{user.username}" if user.username else "без username"
    value = f"{full_name or 'Пользователь'} · {username} · {user.id}"
    return " ".join(value.split())[:128]


def topic_header(user: User) -> str:
    full_name = html.escape(
        " ".join(
            part for part in (user.first_name, user.last_name) if part
        ).strip()
        or "Пользователь"
    )
    username = (
        f"@{html.escape(user.username)}"
        if user.username
        else "не указан"
    )

    return (
        "<b>Карточка подписчика</b>\n"
        f"Имя: {full_name}\n"
        f"Username: {username}\n"
        f"ID: <code>{user.id}</code>"
    )


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Очистить ветки до вчерашнего дня",
                    callback_data="tenant:cleanup_before_yesterday",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Обновить статус",
                    callback_data="tenant:panel_refresh",
                )
            ],
        ]
    )


def is_missing_or_closed_topic_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "message thread not found",
        "message thread is not found",
        "forum topic not found",
        "topic_closed",
        "topic closed",
    )
    return any(marker in text for marker in markers)


def message_is_admin_command(message: Message) -> bool:
    text = message.text or ""
    if not text.startswith("/"):
        return False

    command = text[1:].split(maxsplit=1)[0]
    command = command.split("@", maxsplit=1)[0].lower()
    return command in ADMIN_COMMANDS


class AlbumBuffer:
    """
    Telegram присылает элементы медиагруппы отдельными updates.
    Небольшая задержка позволяет собрать их и потом вызвать copyMessages,
    сохранив исходную группировку альбома.
    """

    def __init__(
        self,
        *,
        delay: float,
        callback: Callable[[list["BufferedMessage"]], Awaitable[None]],
    ) -> None:
        self.delay = delay
        self.callback = callback
        self._groups: dict[Hashable, list[BufferedMessage]] = {}
        self._tasks: dict[Hashable, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        *,
        key: Hashable | None,
        item: "BufferedMessage",
    ) -> None:
        if key is None:
            await self.callback([item])
            return

        async with self._lock:
            self._groups.setdefault(key, []).append(item)
            if key not in self._tasks:
                self._tasks[key] = asyncio.create_task(
                    self._flush_later(key)
                )

    async def _flush_later(self, key: Hashable) -> None:
        try:
            await asyncio.sleep(self.delay)

            async with self._lock:
                items = self._groups.pop(key, [])
                self._tasks.pop(key, None)

            if items:
                items.sort(key=lambda item: item.message.message_id)
                await self.callback(items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработки медиагруппы %r", key)

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(slots=True)
class BufferedMessage:
    message: Message
    owner_id: int
    group_id: int
    user_id: int
    topic_id: int | None = None


class FeedbackRuntime:
    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
        media_group_delay: float,
    ) -> None:
        self.bot = bot
        self.db = db

        self._topic_locks: defaultdict[
            tuple[int, int], asyncio.Lock
        ] = defaultdict(asyncio.Lock)

        self.user_albums = AlbumBuffer(
            delay=media_group_delay,
            callback=self._flush_user_messages,
        )
        self.admin_albums = AlbumBuffer(
            delay=media_group_delay,
            callback=self._flush_admin_messages,
        )

    async def close(self) -> None:
        await self.user_albums.close()
        await self.admin_albums.close()

    async def remember_user(self, user: User) -> None:
        await self.db.upsert_user(
            user_id=user.id,
            first_name=user.first_name or "Пользователь",
            last_name=user.last_name,
            username=user.username,
        )

    async def get_or_create_topic(
        self,
        *,
        owner_id: int,
        user: User,
    ) -> tuple[int, int]:
        tenant = await self.db.get_tenant_by_owner(owner_id)
        if tenant is None:
            raise RuntimeError("Tenant больше не существует")

        existing = await self.db.get_topic_for_user(
            owner_id=owner_id,
            user_id=user.id,
        )
        if existing is not None:
            return int(existing["group_id"]), int(existing["topic_id"])

        lock_key = (owner_id, user.id)

        async with self._topic_locks[lock_key]:
            existing = await self.db.get_topic_for_user(
                owner_id=owner_id,
                user_id=user.id,
            )
            if existing is not None:
                return int(existing["group_id"]), int(existing["topic_id"])

            group_id = int(tenant["group_id"])

            topic = await self.bot.create_forum_topic(
                chat_id=group_id,
                name=topic_name(user),
            )

            await self.db.create_topic_mapping(
                owner_id=owner_id,
                user_id=user.id,
                group_id=group_id,
                topic_id=topic.message_thread_id,
            )

            await self.bot.send_message(
                chat_id=group_id,
                message_thread_id=topic.message_thread_id,
                text=topic_header(user),
                disable_web_page_preview=True,
            )

            return group_id, topic.message_thread_id

    async def accept_user_message(
        self,
        *,
        message: Message,
        owner_id: int,
        group_id: int,
    ) -> None:
        if not message.from_user:
            return

        await self.remember_user(message.from_user)
        await self.db.touch_subscriber(
            owner_id=owner_id,
            user_id=message.from_user.id,
        )

        key = None
        if message.media_group_id:
            key = (
                "user",
                owner_id,
                message.chat.id,
                message.media_group_id,
            )

        await self.user_albums.push(
            key=key,
            item=BufferedMessage(
                message=message,
                owner_id=owner_id,
                group_id=group_id,
                user_id=message.from_user.id,
            ),
        )

    async def _copy_user_batch_to_topic(
        self,
        *,
        items: list[BufferedMessage],
        group_id: int,
        topic_id: int,
    ) -> None:
        first = items[0].message
        message_ids = sorted(
            item.message.message_id for item in items
        )

        if len(message_ids) == 1:
            await self.bot.copy_message(
                chat_id=group_id,
                message_thread_id=topic_id,
                from_chat_id=first.chat.id,
                message_id=message_ids[0],
            )
        else:
            await self.bot.copy_messages(
                chat_id=group_id,
                message_thread_id=topic_id,
                from_chat_id=first.chat.id,
                message_ids=message_ids,
            )

    async def _flush_user_messages(
        self,
        items: list[BufferedMessage],
    ) -> None:
        first = items[0]
        user = first.message.from_user
        if user is None:
            return

        try:
            group_id, topic_id = await self.get_or_create_topic(
                owner_id=first.owner_id,
                user=user,
            )

            try:
                await self._copy_user_batch_to_topic(
                    items=items,
                    group_id=group_id,
                    topic_id=topic_id,
                )
            except TelegramBadRequest as exc:
                if not is_missing_or_closed_topic_error(exc):
                    raise

                # Тему могли вручную удалить/закрыть в Telegram.
                # Сбрасываем mapping и автоматически создаём новую.
                await self.db.delete_topic_mapping(
                    owner_id=first.owner_id,
                    user_id=first.user_id,
                )

                group_id, topic_id = await self.get_or_create_topic(
                    owner_id=first.owner_id,
                    user=user,
                )

                await self._copy_user_batch_to_topic(
                    items=items,
                    group_id=group_id,
                    topic_id=topic_id,
                )

            await self.db.touch_topic(
                owner_id=first.owner_id,
                user_id=first.user_id,
            )

        except TelegramForbiddenError:
            logger.exception(
                "Бот потерял доступ к группе tenant=%s",
                first.owner_id,
            )
            try:
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=(
                        "Сейчас предложка этого канала недоступна. "
                        "Попробуйте отправить сообщение позже."
                    ),
                )
            except Exception:
                pass

        except TelegramBadRequest as exc:
            logger.exception(
                "Не удалось передать сообщение tenant=%s user=%s: %s",
                first.owner_id,
                first.user_id,
                exc,
            )
            try:
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=(
                        "Не удалось передать этот тип сообщения. "
                        "Попробуйте отправить его в другом формате."
                    ),
                )
            except Exception:
                pass

    async def accept_admin_message(
        self,
        *,
        message: Message,
        owner_id: int,
        user_id: int,
        group_id: int,
        topic_id: int,
    ) -> None:
        key = None
        if message.media_group_id:
            key = (
                "admin",
                group_id,
                topic_id,
                message.media_group_id,
            )

        await self.admin_albums.push(
            key=key,
            item=BufferedMessage(
                message=message,
                owner_id=owner_id,
                group_id=group_id,
                user_id=user_id,
                topic_id=topic_id,
            ),
        )

    async def _flush_admin_messages(
        self,
        items: list[BufferedMessage],
    ) -> None:
        first = items[0]
        message_ids = sorted(
            item.message.message_id for item in items
        )

        try:
            if len(message_ids) == 1:
                await self.bot.copy_message(
                    chat_id=first.user_id,
                    from_chat_id=first.group_id,
                    message_id=message_ids[0],
                )
            else:
                await self.bot.copy_messages(
                    chat_id=first.user_id,
                    from_chat_id=first.group_id,
                    message_ids=message_ids,
                )

            await self.db.set_user_blocked(first.user_id, False)
            await self.db.touch_topic(
                owner_id=first.owner_id,
                user_id=first.user_id,
            )

        except TelegramForbiddenError:
            await self.db.set_user_blocked(first.user_id, True)

            try:
                await self.bot.send_message(
                    chat_id=first.group_id,
                    message_thread_id=first.topic_id,
                    text=(
                        "Не удалось доставить ответ: пользователь "
                        "заблокировал бота или запретил личные сообщения."
                    ),
                )
            except Exception:
                pass

        except TelegramBadRequest as exc:
            logger.exception(
                "Ошибка отправки ответа user=%s: %s",
                first.user_id,
                exc,
            )

            try:
                await self.bot.send_message(
                    chat_id=first.group_id,
                    message_thread_id=first.topic_id,
                    text=(
                        "Не удалось доставить ответ пользователю: "
                        f"{html.escape(str(exc))}"
                    ),
                )
            except Exception:
                pass


class TopicCleaner:
    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
    ) -> None:
        self.bot = bot
        self.db = db
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def cleanup_rows(
        self,
        *,
        owner_id: int,
        rows,
    ) -> dict[str, int]:
        result = {
            "deleted": 0,
            "closed": 0,
            "stale": 0,
            "failed": 0,
        }

        async with self._locks[owner_id]:
            for row in rows:
                status = await self._remove_topic(row)
                result[status] += 1
                await asyncio.sleep(0.05)

        return result

    async def _remove_topic(self, row) -> str:
        owner_id = int(row["owner_id"])
        user_id = int(row["user_id"])
        group_id = int(row["group_id"])
        topic_id = int(row["topic_id"])

        try:
            await self.bot.delete_forum_topic(
                chat_id=group_id,
                message_thread_id=topic_id,
            )
            await self.db.delete_topic_mapping(
                owner_id=owner_id,
                user_id=user_id,
            )
            return "deleted"

        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "deleteForumTopic не сработал group=%s topic=%s: %s",
                group_id,
                topic_id,
                exc,
            )

            if is_missing_or_closed_topic_error(exc):
                # Если тема уже отсутствует, stale mapping больше не нужен.
                if "not found" in str(exc).lower():
                    await self.db.delete_topic_mapping(
                        owner_id=owner_id,
                        user_id=user_id,
                    )
                    return "stale"

        try:
            await self.bot.close_forum_topic(
                chat_id=group_id,
                message_thread_id=topic_id,
            )
            await self.db.delete_topic_mapping(
                owner_id=owner_id,
                user_id=user_id,
            )
            return "closed"

        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if is_missing_or_closed_topic_error(exc):
                await self.db.delete_topic_mapping(
                    owner_id=owner_id,
                    user_id=user_id,
                )
                return "stale"

            logger.error(
                "Не удалось удалить/закрыть group=%s topic=%s: %s",
                group_id,
                topic_id,
                exc,
            )
            return "failed"

    async def cleanup_created_before(
        self,
        *,
        owner_id: int,
        cutoff: datetime,
    ) -> dict[str, int]:
        rows = await self.db.topics_created_before(
            owner_id=owner_id,
            cutoff=cutoff,
        )
        return await self.cleanup_rows(
            owner_id=owner_id,
            rows=rows,
        )


class AdminGuard:
    def __init__(
        self,
        *,
        bot: Bot,
        ttl_seconds: int = 300,
    ) -> None:
        self.bot = bot
        self.ttl_seconds = ttl_seconds
        self._cache: dict[
            tuple[int, int],
            tuple[bool, datetime],
        ] = {}

    async def is_group_admin(
        self,
        message: Message,
        group_id: int,
    ) -> bool:
        # Анонимный администратор, отправляющий от имени самой группы.
        if message.sender_chat and message.sender_chat.id == group_id:
            return True

        if not message.from_user:
            return False

        key = (group_id, message.from_user.id)
        now = utc_now()
        cached = self._cache.get(key)

        if cached and cached[1] > now:
            return cached[0]

        try:
            member = await self.bot.get_chat_member(
                group_id,
                message.from_user.id,
            )
            allowed = member.status in {
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
            }
        except Exception:
            logger.exception(
                "Не удалось проверить администратора group=%s user=%s",
                group_id,
                message.from_user.id,
            )
            allowed = False

        self._cache[key] = (
            allowed,
            now + timedelta(seconds=self.ttl_seconds),
        )
        return allowed


def _manual_cutoff(timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    now_local = datetime.now(tz)

    # Формулировка ТЗ соблюдается буквально:
    # "созданные ДО 00:00 вчерашнего дня".
    # То есть весь вчерашний и сегодняшний день сохраняются.
    yesterday = now_local.date() - timedelta(days=1)

    cutoff_local = datetime.combine(
        yesterday,
        time.min,
        tzinfo=tz,
    )
    return cutoff_local.astimezone(timezone.utc)


async def _owner_tenant_in_group(
    *,
    message: Message,
    db: Database,
) -> tuple[object | None, str | None]:
    if message.chat.type != ChatType.SUPERGROUP:
        return None, "Команда работает только в привязанной супергруппе."

    tenant = await db.get_tenant_by_group(message.chat.id)
    if tenant is None:
        return None, "Эта супергруппа ещё не настроена. Используйте /setup."

    if not message.from_user:
        return None, "Команду должен отправить обычный Telegram-аккаунт."

    if int(tenant["owner_id"]) != message.from_user.id:
        return None, "Эту настройку может менять только владелец, выполнивший /setup."

    return tenant, None


async def _panel_text(
    *,
    bot: Bot,
    db: Database,
    tenant,
) -> str:
    owner_id = int(tenant["owner_id"])
    subscribers = await db.count_tenant_subscribers(owner_id)
    topics = await db.count_tenant_topics(owner_id)

    link = await create_start_link(
        bot,
        f"ref_{owner_id}",
        encode=False,
    )

    tz = ZoneInfo(str(tenant["timezone_name"]))
    next_reset = dt_from_db(
        str(tenant["next_reset_at"])
    ).astimezone(tz)

    return (
        "<b>Панель предложки</b>\n\n"
        f"Группа: {html.escape(str(tenant['group_title']))}\n"
        f"Владелец: <code>{owner_id}</code>\n"
        f"Подписчиков: <b>{subscribers}</b>\n"
        f"Активных тем: <b>{topics}</b>\n\n"
        f"Период авто-сброса: "
        f"<b>{int(tenant['reset_interval_days'])} дней</b>\n"
        f"Часовой пояс: "
        f"<code>{html.escape(str(tenant['timezone_name']))}</code>\n"
        f"Следующий сброс: <b>{next_reset:%d.%m.%Y %H:%M}</b>\n\n"
        "<b>Диплинк:</b>\n"
        f"<code>{html.escape(link)}</code>\n\n"
        "<b>Предупреждение за 24 часа:</b>\n"
        f"{html.escape(str(tenant['notice_text']))}\n\n"
        "<code>/set_period 30</code> — период\n"
        "<code>/set_announcement текст</code> — анонс\n"
        "<code>/set_timezone Europe/Moscow</code> — часовой пояс"
    )


def register_handlers(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    runtime: FeedbackRuntime,
    cleaner: TopicCleaner,
    settings,
) -> None:
    router = Router(name="feedback")
    guard = AdminGuard(bot=bot)

    # --------------------------------------------------------------
    # /setup
    # --------------------------------------------------------------

    @router.message(Command("setup"))
    async def setup_handler(message: Message) -> None:
        if message.chat.type != ChatType.SUPERGROUP:
            await message.answer(
                "Команду /setup нужно отправить в закрытой супергруппе "
                "с включёнными Темами."
            )
            return

        chat = await bot.get_chat(message.chat.id)
        if not getattr(chat, "is_forum", False):
            await message.answer(
                "В этой супергруппе не включены Темы (Forum Topics). "
                "Сначала включите их в настройках группы."
            )
            return

        if not message.from_user:
            await message.answer(
                "/setup нельзя выполнять анонимно. "
                "Отправьте команду от своего аккаунта."
            )
            return

        caller = await bot.get_chat_member(
            message.chat.id,
            message.from_user.id,
        )
        if caller.status not in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }:
            await message.answer(
                "/setup может выполнить только администратор этой группы."
            )
            return

        me = await bot.get_me()
        bot_member = await bot.get_chat_member(
            message.chat.id,
            me.id,
        )

        bot_is_creator = bot_member.status == ChatMemberStatus.CREATOR
        bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR

        if not bot_is_creator and not bot_is_admin:
            await message.answer(
                "Сначала сделайте бота администратором этой супергруппы."
            )
            return

        if (
            not bot_is_creator
            and not bool(getattr(bot_member, "can_manage_topics", False))
        ):
            await message.answer(
                "Боту не хватает права <b>Управление темами / Manage Topics</b>. "
                "Выдайте это право и повторите /setup."
            )
            return

        status, tenant = await db.register_tenant(
            owner_id=message.from_user.id,
            group_id=message.chat.id,
            group_title=message.chat.title or str(message.chat.id),
            default_reset_days=settings.default_reset_days,
            default_notice_text=settings.default_notice_text,
            default_timezone=settings.default_timezone,
        )

        if status == "owner_has_other_group":
            await message.answer(
                "Ваш аккаунт уже привязан к другой супергруппе. "
                "В этой версии один владелец может иметь одну предложку."
            )
            return

        if status == "group_has_other_owner":
            await message.answer(
                "Эта супергруппа уже зарегистрирована другим владельцем."
            )
            return

        link = await create_start_link(
            bot,
            f"ref_{message.from_user.id}",
            encode=False,
        )

        can_delete = bot_is_creator or bool(
            getattr(bot_member, "can_delete_messages", False)
        )

        warning = ""
        if not can_delete:
            warning = (
                "\n\n<b>Предупреждение:</b> у бота нет права "
                "<b>Удаление сообщений / Delete Messages</b>. "
                "Создание тем будет работать, но при очистке бот сможет "
                "только попытаться закрыть тему вместо полного удаления."
            )

        action = (
            "Предложка создана."
            if status == "created"
            else "Настройка уже существовала и обновлена."
        )

        await message.answer(
            f"<b>{action}</b>\n\n"
            "Персональная ссылка для подписчиков:\n"
            f"<code>{html.escape(link)}</code>\n\n"
            "Опубликуйте её в своём канале. "
            "Подписчики, открывшие ссылку, будут направляться "
            "именно в эту супергруппу."
            f"{warning}",
            disable_web_page_preview=True,
        )

    # --------------------------------------------------------------
    # Tenant panel and settings
    # --------------------------------------------------------------

    @router.message(Command("panel"))
    async def panel_handler(message: Message) -> None:
        tenant, error = await _owner_tenant_in_group(
            message=message,
            db=db,
        )
        if error:
            await message.answer(error)
            return

        await message.answer(
            await _panel_text(
                bot=bot,
                db=db,
                tenant=tenant,
            ),
            reply_markup=panel_keyboard(),
            disable_web_page_preview=True,
        )

    @router.message(Command("set_period"))
    async def set_period_handler(
        message: Message,
        command: CommandObject,
    ) -> None:
        tenant, error = await _owner_tenant_in_group(
            message=message,
            db=db,
        )
        if error:
            await message.answer(error)
            return

        try:
            days = int((command.args or "").strip())
            if days < 2 or days > 3650:
                raise ValueError
        except ValueError:
            await message.answer(
                "Использование: <code>/set_period 30</code>\n"
                "Допустимо от 2 до 3650 дней."
            )
            return

        next_reset = await db.set_tenant_period(
            int(tenant["owner_id"]),
            days,
        )

        tz = ZoneInfo(str(tenant["timezone_name"]))
        local_reset = next_reset.astimezone(tz)

        await message.answer(
            f"Период установлен: <b>{days} дней</b>.\n"
            "Новый отсчёт начат сейчас.\n"
            f"Следующий сброс: <b>{local_reset:%d.%m.%Y %H:%M}</b>."
        )

    @router.message(Command("set_announcement"))
    async def set_announcement_handler(
        message: Message,
        command: CommandObject,
    ) -> None:
        tenant, error = await _owner_tenant_in_group(
            message=message,
            db=db,
        )
        if error:
            await message.answer(error)
            return

        text = (command.args or "").strip()
        if not text:
            await message.answer(
                "Использование:\n"
                "<code>/set_announcement Текст предупреждения</code>"
            )
            return

        if len(text) > 4000:
            await message.answer(
                "Текст слишком длинный. Максимум 4000 символов."
            )
            return

        await db.set_tenant_notice(
            int(tenant["owner_id"]),
            text,
        )
        await message.answer("Текст предупреждения сохранён.")

    @router.message(Command("set_timezone"))
    async def set_timezone_handler(
        message: Message,
        command: CommandObject,
    ) -> None:
        tenant, error = await _owner_tenant_in_group(
            message=message,
            db=db,
        )
        if error:
            await message.answer(error)
            return

        timezone_name = (command.args or "").strip()
        if not timezone_name:
            await message.answer(
                "Использование: "
                "<code>/set_timezone Asia/Tashkent</code>"
            )
            return

        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            await message.answer(
                "Неизвестный часовой пояс. Используйте IANA-имя, "
                "например <code>Asia/Tashkent</code> "
                "или <code>Europe/Moscow</code>."
            )
            return

        await db.set_tenant_timezone(
            int(tenant["owner_id"]),
            timezone_name,
        )
        await message.answer(
            "Часовой пояс сохранён: "
            f"<code>{html.escape(timezone_name)}</code>"
        )

    # --------------------------------------------------------------
    # Panel callbacks
    # --------------------------------------------------------------

    @router.callback_query(F.data == "tenant:panel_refresh")
    async def panel_refresh(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer()
            return

        tenant = await db.get_tenant_by_group(
            callback.message.chat.id
        )
        if tenant is None:
            await callback.answer(
                "Группа больше не зарегистрирована.",
                show_alert=True,
            )
            return

        if callback.from_user.id != int(tenant["owner_id"]):
            await callback.answer(
                "Эта панель доступна только владельцу.",
                show_alert=True,
            )
            return

        await callback.message.edit_text(
            await _panel_text(
                bot=bot,
                db=db,
                tenant=tenant,
            ),
            reply_markup=panel_keyboard(),
            disable_web_page_preview=True,
        )
        await callback.answer("Обновлено.")

    @router.callback_query(
        F.data == "tenant:cleanup_before_yesterday"
    )
    async def manual_cleanup(callback: CallbackQuery) -> None:
        if callback.message is None:
            await callback.answer()
            return

        tenant = await db.get_tenant_by_group(
            callback.message.chat.id
        )
        if tenant is None:
            await callback.answer(
                "Группа больше не зарегистрирована.",
                show_alert=True,
            )
            return

        if callback.from_user.id != int(tenant["owner_id"]):
            await callback.answer(
                "Очистку может запускать только владелец.",
                show_alert=True,
            )
            return

        await callback.answer("Очистка запущена.")

        cutoff = _manual_cutoff(str(tenant["timezone_name"]))
        result = await cleaner.cleanup_created_before(
            owner_id=int(tenant["owner_id"]),
            cutoff=cutoff,
        )

        local_cutoff = cutoff.astimezone(
            ZoneInfo(str(tenant["timezone_name"]))
        )

        processed = (
            result["deleted"]
            + result["closed"]
            + result["stale"]
        )

        await callback.message.answer(
            "<b>Ручная очистка завершена.</b>\n"
            f"Обработано: <b>{processed}</b>\n"
            f"Удалено: {result['deleted']}\n"
            f"Закрыто: {result['closed']}\n"
            f"Уже отсутствовали: {result['stale']}\n"
            f"Ошибок: {result['failed']}\n\n"
            "Граница по created_at: раньше "
            f"<b>{local_cutoff:%d.%m.%Y %H:%M}</b>."
        )

    # --------------------------------------------------------------
    # Public /start with tenant deep link
    # --------------------------------------------------------------

    @router.message(
        CommandStart(),
        F.chat.type == ChatType.PRIVATE,
    )
    async def start_handler(
        message: Message,
        command: CommandObject,
    ) -> None:
        if not message.from_user:
            return

        await runtime.remember_user(message.from_user)

        payload = (command.args or "").strip()

        if not payload:
            active = await db.get_active_tenant_for_user(
                message.from_user.id
            )
            if active is None:
                await message.answer(
                    "Откройте бота по ссылке из нужного Telegram-канала. "
                    "После этого сюда можно будет отправлять сообщения."
                )
            else:
                await message.answer(
                    "Предложка активна. Отправьте сюда текст, фото, "
                    "видео, голосовое сообщение, документ или альбом."
                )
            return

        if not payload.startswith("ref_"):
            await message.answer("Некорректная ссылка предложки.")
            return

        owner_raw = payload[4:]
        if not owner_raw.isdigit():
            await message.answer("Некорректная ссылка предложки.")
            return

        owner_id = int(owner_raw)
        tenant = await db.get_tenant_by_owner(owner_id)

        if tenant is None:
            await message.answer(
                "Эта ссылка больше не активна или предложка не настроена."
            )
            return

        await db.attach_subscriber(
            owner_id=owner_id,
            user_id=message.from_user.id,
        )

        await message.answer(
            "Вы подключены к предложке. "
            "Отправьте сюда текст, фото, видео, голосовое сообщение, "
            "документ или альбом. Ответ администратора придёт сюда же."
        )

    # --------------------------------------------------------------
    # Subscriber messages
    # --------------------------------------------------------------

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def subscriber_message_handler(message: Message) -> None:
        if not message.from_user:
            return

        tenant = await db.get_active_tenant_for_user(
            message.from_user.id
        )
        if tenant is None:
            await message.answer(
                "Сначала откройте бота по персональной ссылке "
                "из нужного Telegram-канала."
            )
            return

        await runtime.accept_user_message(
            message=message,
            owner_id=int(tenant["owner_id"]),
            group_id=int(tenant["group_id"]),
        )

    # --------------------------------------------------------------
    # Messages from tenant admin groups to subscribers
    # --------------------------------------------------------------

    @router.message(F.chat.type == ChatType.SUPERGROUP)
    async def admin_group_message_handler(message: Message) -> None:
        tenant = await db.get_tenant_by_group(message.chat.id)
        if tenant is None:
            return

        # Сервисные сообщения о темах пользователю не отправляем.
        if (
            message.forum_topic_created
            or message.forum_topic_closed
            or message.forum_topic_reopened
            or message.forum_topic_edited
        ):
            return

        if not message.message_thread_id:
            return

        if message_is_admin_command(message):
            return

        # Сообщения самого бота (карточка пользователя, ошибки доставки,
        # панель и т.п.) нельзя отправлять обратно подписчику.
        if (
            message.from_user
            and message.from_user.is_bot
            and not (
                message.sender_chat
                and message.sender_chat.id == message.chat.id
            )
        ):
            return

        topic = await db.get_topic_by_group_thread(
            group_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        if topic is None:
            return

        if not await guard.is_group_admin(
            message,
            message.chat.id,
        ):
            return

        await runtime.accept_admin_message(
            message=message,
            owner_id=int(topic["owner_id"]),
            user_id=int(topic["user_id"]),
            group_id=message.chat.id,
            topic_id=int(topic["topic_id"]),
        )

    dispatcher.include_router(router)
