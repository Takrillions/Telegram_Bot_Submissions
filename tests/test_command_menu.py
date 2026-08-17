import unittest

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
)

from command_menu import (
    GENERAL_OWNER_COMMANDS,
    OWNER_COMMANDS,
    SUPERADMIN_COMMANDS,
    SUPERADMIN_OWNER_COMMANDS,
    TOPIC_ADMIN_COMMANDS,
    USER_COMMANDS,
    sync_command_menus,
)


class _Database:
    async def list_enabled_channels(self):
        return [
            {"owner_id": 9, "group_id": -1009},
            {"owner_id": 4, "group_id": -1004},
            {"owner_id": 9, "group_id": -1099},
        ]


class _Bot:
    def __init__(self, *, live_admins=None):
        self.calls = []
        self.live_admins = set(live_admins or {(-1009, 9), (-1004, 4), (-1099, 9)})

    async def set_my_commands(self, commands, *, scope):
        self.calls.append(("set", commands, scope))

    async def delete_my_commands(self, *, scope):
        self.calls.append(("delete", None, scope))

    async def get_chat_member(self, group_id, user_id):
        status = "administrator" if (group_id, user_id) in self.live_admins else "member"
        return type("Member", (), {"status": status})()


class _BotWithStaleGroup(_Bot):
    async def delete_my_commands(self, *, scope):
        self.calls.append(("delete", None, scope))
        if (
            isinstance(scope, BotCommandScopeChat)
            and int(scope.chat_id) == -1009
        ):
            raise TelegramForbiddenError(
                method=None,
                message="Forbidden: bot was kicked from the supergroup chat",
            )

    async def get_chat_member(self, group_id, user_id):
        if int(group_id) == -1009:
            raise TelegramForbiddenError(
                method=None,
                message="Forbidden: bot was kicked from the supergroup chat",
            )
        return await super().get_chat_member(group_id, user_id)


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_supergroup_does_not_abort_command_menu_sync(self):
        bot = _BotWithStaleGroup()

        await sync_command_menus(
            bot=bot,
            db=_Database(),
            superadmin_telegram_id=9,
        )

        private_sets = [
            call
            for call in bot.calls
            if call[0] == "set"
            and (
                isinstance(call[2], BotCommandScopeAllPrivateChats)
                or (
                    isinstance(call[2], BotCommandScopeChat)
                    and int(call[2].chat_id) > 0
                )
            )
        ]
        self.assertTrue(private_sets)

    async def test_only_private_scopes_are_ever_set(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=9)

        set_calls = [call for call in bot.calls if call[0] == "set"]
        self.assertTrue(set_calls)
        for _, _, scope in set_calls:
            self.assertTrue(
                isinstance(scope, BotCommandScopeAllPrivateChats)
                or (isinstance(scope, BotCommandScopeChat) and int(scope.chat_id) > 0),
                f"unexpected published command scope: {scope!r}",
            )

    async def test_legacy_group_and_default_scopes_are_explicitly_deleted(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=None)

        deleted_scopes = [scope for action, _, scope in bot.calls if action == "delete"]
        self.assertTrue(any(isinstance(scope, BotCommandScopeDefault) for scope in deleted_scopes))
        self.assertTrue(any(isinstance(scope, BotCommandScopeAllGroupChats) for scope in deleted_scopes))
        self.assertTrue(any(isinstance(scope, BotCommandScopeAllChatAdministrators) for scope in deleted_scopes))

        group_chat_deletes = {
            int(scope.chat_id)
            for scope in deleted_scopes
            if isinstance(scope, BotCommandScopeChat) and int(scope.chat_id) < 0
        }
        group_admin_deletes = {
            int(scope.chat_id)
            for scope in deleted_scopes
            if isinstance(scope, BotCommandScopeChatAdministrators)
        }
        self.assertEqual(group_chat_deletes, {-1004, -1009, -1099})
        self.assertEqual(group_admin_deletes, {-1004, -1009, -1099})

    async def test_private_user_and_owner_scopes_are_registered_once_per_owner(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=None)

        private_sets = [
            call for call in bot.calls
            if call[0] == "set" and isinstance(call[2], BotCommandScopeAllPrivateChats)
        ]
        self.assertEqual(len(private_sets), 1)
        self.assertEqual(
            [item.command for item in private_sets[0][1]],
            [item.command for item in USER_COMMANDS],
        )

        owner_sets = [
            call for call in bot.calls
            if call[0] == "set"
            and isinstance(call[2], BotCommandScopeChat)
            and int(call[2].chat_id) > 0
        ]
        self.assertEqual({int(call[2].chat_id) for call in owner_sets}, {4, 9})
        self.assertTrue(
            all(
                [item.command for item in call[1]]
                == [item.command for item in OWNER_COMMANDS]
                for call in owner_sets
            )
        )

    async def test_superadmin_owner_gets_merged_private_menu(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=9)
        scope_sets = [
            call for call in bot.calls
            if call[0] == "set"
            and isinstance(call[2], BotCommandScopeChat)
            and int(call[2].chat_id) == 9
        ]
        self.assertEqual(len(scope_sets), 1)
        self.assertEqual(
            [item.command for item in scope_sets[0][1]],
            [item.command for item in SUPERADMIN_OWNER_COMMANDS],
        )
        self.assertEqual([item.command for item in scope_sets[0][1]].count("superadmin"), 1)

    async def test_superadmin_without_channel_gets_user_plus_superadmin_only(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=777)
        scope_sets = [
            call for call in bot.calls
            if call[0] == "set"
            and isinstance(call[2], BotCommandScopeChat)
            and int(call[2].chat_id) == 777
        ]
        self.assertEqual(len(scope_sets), 1)
        self.assertEqual(
            [item.command for item in scope_sets[0][1]],
            [item.command for item in SUPERADMIN_COMMANDS],
        )
        self.assertNotIn("panel", [item.command for item in scope_sets[0][1]])

    async def test_removed_owner_falls_back_to_ordinary_private_menu(self):
        bot = _Bot(live_admins={(-1004, 4)})
        await sync_command_menus(bot=bot, db=_Database(), superadmin_telegram_id=None)
        private_actor_sets = [
            call for call in bot.calls
            if call[0] == "set"
            and isinstance(call[2], BotCommandScopeChat)
            and int(call[2].chat_id) > 0
        ]
        self.assertEqual({int(call[2].chat_id) for call in private_actor_sets}, {4})
        deleted_private_9 = [
            call for call in bot.calls
            if call[0] == "delete"
            and isinstance(call[2], BotCommandScopeChat)
            and int(call[2].chat_id) == 9
        ]
        self.assertEqual(len(deleted_private_9), 1)

    def test_command_levels_are_explicit_and_group_commands_remain_manual(self):
        user = {item.command for item in USER_COMMANDS}
        owner = {item.command for item in OWNER_COMMANDS}
        superadmin = {item.command for item in SUPERADMIN_COMMANDS}
        self.assertEqual(user, {"start", "channels", "privacy"})
        self.assertTrue(user < owner)
        self.assertEqual(superadmin, user | {"superadmin"})
        self.assertIn("setup", GENERAL_OWNER_COMMANDS)
        self.assertIn("broadcast", GENERAL_OWNER_COMMANDS)
        self.assertIn("subscriber", TOPIC_ADMIN_COMMANDS)
        self.assertNotIn("setup", user)


if __name__ == "__main__":
    unittest.main()
