import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS_PATH = ROOT / "handlers.py"
COMMAND_MENU_PATH = ROOT / "command_menu.py"
HANDLERS = HANDLERS_PATH.read_text(encoding="utf-8")
COMMAND_MENU = COMMAND_MENU_PATH.read_text(encoding="utf-8")
HANDLERS_TREE = ast.parse(HANDLERS)


def function_source(name: str) -> str:
    node = next(
        item for item in ast.walk(HANDLERS_TREE)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(HANDLERS, node) or ""


class CommandBoundaryStaticTests(unittest.TestCase):
    def test_group_owner_commands_are_general_only(self):
        source = function_source("_owner_channel_in_group")
        self.assertIn("if not is_general_forum_message(message)", source)

    def test_setup_is_general_only(self):
        source = function_source("setup_handler")
        self.assertIn("if not is_general_forum_message(message)", source)

    def test_subscriber_topic_commands_require_topic_mapping(self):
        for name in ("subscriber_history_handler", "subscriber_handler", "status_handler"):
            self.assertIn("get_topic_by_group_thread", function_source(name), name)

    def test_command_like_admin_messages_never_fall_through_to_subscriber_reply(self):
        source = function_source("message_is_admin_command")
        self.assertIn('text.startswith("/")', source)
        self.assertNotIn("command in ADMIN_COMMANDS", source)
        generic = function_source("admin_group_message_handler")
        self.assertIn("if message_is_admin_command(message)", generic)

    def test_command_menu_publishes_private_scopes_and_only_deletes_group_scopes(self):
        self.assertIn("BotCommandScopeAllPrivateChats", COMMAND_MENU)
        self.assertIn("async def _clear_group_command_scopes", COMMAND_MENU)
        clear_source = COMMAND_MENU[
            COMMAND_MENU.index("async def _clear_group_command_scopes"):
            COMMAND_MENU.index("async def _has_live_owner_role")
        ]
        self.assertIn("BotCommandScopeAllGroupChats", clear_source)
        self.assertIn("BotCommandScopeChatAdministrators", clear_source)
        self.assertIn("BotCommandScopeAllChatAdministrators", clear_source)
        self.assertNotIn("set_my_commands", clear_source)

        sync_source = COMMAND_MENU[COMMAND_MENU.index("async def sync_command_menus"):]
        self.assertIn("BotCommandScopeAllPrivateChats", sync_source)
        self.assertIn("BotCommandScopeChat(chat_id=actor_id)", sync_source)
        self.assertNotIn("set_my_commands(list(GENERAL_OWNER_COMMANDS)", sync_source)
        self.assertNotIn("set_my_commands(list(TOPIC_ADMIN_COMMANDS)", sync_source)


if __name__ == "__main__":
    unittest.main()
