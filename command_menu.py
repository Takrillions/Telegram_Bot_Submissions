"""Telegram command registry and private-only command-menu synchronisation.

Telegram Bot API command scopes still cannot target a forum
``message_thread_id``.  A menu published for a supergroup is therefore visible
in General *and* subscriber topics.  The product rule is stricter: command
suggestions after ``/`` exist only in private chats.  Group commands continue
to work when typed manually and are protected by handler context checks.

``sync_command_menus`` also removes legacy/default group scopes before
publishing private menus.  This matters because Telegram falls back to broader
scopes when a narrower one is absent; merely stopping future group
``setMyCommands`` calls would not remove commands that were published by an
older release.
"""

from __future__ import annotations

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
)


USER_COMMANDS = (
    BotCommand(command="start", description="Начать работу с ботом"),
    BotCommand(command="channels", description="Выбрать предложку"),
    BotCommand(command="privacy", description="Изменить режим приватности"),
)

OWNER_COMMANDS = USER_COMMANDS + (
    BotCommand(command="panel", description="Панель управления"),
    BotCommand(command="search", description="Поиск подписчиц и обращений"),
)

SUPERADMIN_COMMAND = BotCommand(
    command="superadmin",
    description="Глобальное управление ботом",
)
SUPERADMIN_COMMANDS = USER_COMMANDS + (SUPERADMIN_COMMAND,)
SUPERADMIN_OWNER_COMMANDS = OWNER_COMMANDS + (SUPERADMIN_COMMAND,)

# Commands accepted manually from the General topic.  These registries do NOT
# publish Telegram command menus in groups.  They remain the source of truth
# for handler/context tests and for a possible future Bot API topic scope.
GENERAL_OWNER_COMMANDS = (
    "setup", "panel", "stats", "set_period", "set_announcement",
    "set_timezone", "set_topic_template", "broadcast",
)
TOPIC_ADMIN_COMMANDS = ("subscriber", "subscriber_history", "status")


async def _clear_group_command_scopes(*, bot, channels) -> None:
    """Remove command scopes that could make ``/`` suggestions visible in groups.

    Telegram command lookup falls back all the way to the default scope.  We
    therefore clear the default/global group scopes as well as explicit scopes
    for every enabled proposal supergroup known to the database.
    """
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())

    group_ids = sorted({int(channel["group_id"]) for channel in channels})
    for group_id in group_ids:
        try:
            await bot.delete_my_commands(
                scope=BotCommandScopeChat(chat_id=group_id)
            )
            await bot.delete_my_commands(
                scope=BotCommandScopeChatAdministrators(chat_id=group_id)
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            # A stale channel may remain in the database after the bot was
            # removed from the supergroup. It must not prevent bot startup.
            continue


async def _has_live_owner_role(*, bot, channels, owner_id: int) -> bool:
    for channel in channels:
        if int(channel["owner_id"]) != owner_id:
            continue
        try:
            member = await bot.get_chat_member(int(channel["group_id"]), owner_id)
        except Exception:
            continue
        if member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }:
            return True
    return False


async def sync_command_menus(
    *,
    bot,
    db,
    superadmin_telegram_id: int | None = None,
) -> None:
    """Synchronise Telegram's command suggestions with the private-only policy.

    Ordinary users receive ``USER_COMMANDS`` in all private chats.  A current
    channel owner receives the owner menu in their private chat.  The configured
    SUPERADMIN receives ``/superadmin`` in that same private scope; when that
    account is also a live channel owner, the two private menus are merged.

    No group/supergroup command list is installed.  Manual General/topic
    commands are unaffected because command menus and command handlers are
    independent Bot API concepts.
    """
    channels = await db.list_enabled_channels()
    await _clear_group_command_scopes(bot=bot, channels=channels)

    await bot.set_my_commands(
        list(USER_COMMANDS),
        scope=BotCommandScopeAllPrivateChats(),
    )

    owner_ids = {int(channel["owner_id"]) for channel in channels}
    superadmin_id = (
        int(superadmin_telegram_id)
        if isinstance(superadmin_telegram_id, int) and superadmin_telegram_id > 0
        else None
    )
    private_actor_ids = set(owner_ids)
    if superadmin_id is not None:
        private_actor_ids.add(superadmin_id)

    for actor_id in sorted(private_actor_ids):
        scope = BotCommandScopeChat(chat_id=actor_id)
        # Remove a stale per-user override first so role changes cannot leave
        # obsolete owner/superadmin commands behind.
        await bot.delete_my_commands(scope=scope)

        is_live_owner = (
            actor_id in owner_ids
            and await _has_live_owner_role(
                bot=bot,
                channels=channels,
                owner_id=actor_id,
            )
        )
        is_superadmin = superadmin_id is not None and actor_id == superadmin_id

        if is_live_owner and is_superadmin:
            commands = SUPERADMIN_OWNER_COMMANDS
        elif is_live_owner:
            commands = OWNER_COMMANDS
        elif is_superadmin:
            commands = SUPERADMIN_COMMANDS
        else:
            # No explicit scope: Telegram falls back to USER_COMMANDS from
            # BotCommandScopeAllPrivateChats.
            continue

        await bot.set_my_commands(list(commands), scope=scope)
