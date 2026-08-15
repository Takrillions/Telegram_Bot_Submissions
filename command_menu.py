"""Telegram command registry and safe command-scope synchronisation.

Telegram Bot API scopes do not include a forum ``message_thread_id``.  We
therefore only publish menus in private chats: publishing a group scope would
also make that menu visible in every subscriber topic, contrary to the product
privacy/UX rule.  Group commands remain available when explicitly typed and
are protected by the handler context checks.
"""

from __future__ import annotations

from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
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

# Commands accepted manually from the General topic.  The list is also the
# source for future Bot API topic-level scopes if Telegram adds that feature.
GENERAL_OWNER_COMMANDS = (
    "setup", "panel", "stats", "set_period", "set_announcement",
    "set_timezone", "set_topic_template", "broadcast",
)
TOPIC_ADMIN_COMMANDS = ("subscriber", "subscriber_history", "status")


async def sync_command_menus(*, bot, db) -> None:
    """Install private command menus for users and current channel owners.

    A per-owner private scope overrides the generic private-user scope.  It is
    deliberately not a group scope, because such a scope cannot be confined to
    the General forum topic by the Telegram API.
    """
    await bot.set_my_commands(list(USER_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    channels = await db.list_enabled_channels()
    owner_ids = {int(channel["owner_id"]) for channel in channels}
    for owner_id in sorted(owner_ids):
        scope = BotCommandScopeChat(chat_id=owner_id)
        # Clear a stale override first.  An owner who lost group admin rights
        # falls back to the ordinary private menu at the next sync.
        await bot.delete_my_commands(scope=scope)
        has_live_owner_role = False
        for channel in channels:
            if int(channel["owner_id"]) != owner_id:
                continue
            try:
                member = await bot.get_chat_member(int(channel["group_id"]), owner_id)
            except Exception:
                continue
            if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
                has_live_owner_role = True
                break
        if has_live_owner_role:
            await bot.set_my_commands(list(OWNER_COMMANDS), scope=scope)
