import unittest

from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat

from command_menu import (
    GENERAL_OWNER_COMMANDS,
    OWNER_COMMANDS,
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
    def __init__(self):
        self.calls = []

    async def set_my_commands(self, commands, *, scope):
        self.calls.append((commands, scope))

    async def delete_my_commands(self, *, scope):
        self.calls.append((None, scope))

    async def get_chat_member(self, group_id, user_id):
        return type("Member", (), {"status": "administrator"})()


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_user_and_owner_scopes_are_registered_once_per_owner(self):
        bot = _Bot()
        await sync_command_menus(bot=bot, db=_Database())
        self.assertEqual(len(bot.calls), 5)
        commands, scope = bot.calls[0]
        self.assertIsInstance(scope, BotCommandScopeAllPrivateChats)
        self.assertEqual([item.command for item in commands], [item.command for item in USER_COMMANDS])
        owner_sets = [call for call in bot.calls[1:] if call[0] is not None]
        self.assertEqual({call[1].chat_id for call in owner_sets if isinstance(call[1], BotCommandScopeChat)}, {4, 9})
        self.assertTrue(all([item.command for item in call[0]] == [item.command for item in OWNER_COMMANDS] for call in owner_sets))

    def test_command_levels_are_explicit_and_do_not_expose_system_commands_to_users(self):
        user = {item.command for item in USER_COMMANDS}
        owner = {item.command for item in OWNER_COMMANDS}
        self.assertEqual(user, {"start", "channels", "privacy"})
        self.assertTrue(user < owner)
        self.assertIn("setup", GENERAL_OWNER_COMMANDS)
        self.assertIn("broadcast", GENERAL_OWNER_COMMANDS)
        self.assertIn("subscriber", TOPIC_ADMIN_COMMANDS)
        self.assertNotIn("setup", user)

