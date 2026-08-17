import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND_MENU = (ROOT / "command_menu.py").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
HANDLERS = (ROOT / "handlers.py").read_text(encoding="utf-8")


class CommandMenuStage12StaticTests(unittest.TestCase):
    def test_superadmin_is_private_command_only(self):
        self.assertIn('command="superadmin"', COMMAND_MENU)
        self.assertIn("SUPERADMIN_COMMANDS = USER_COMMANDS + (SUPERADMIN_COMMAND,)", COMMAND_MENU)
        self.assertIn("SUPERADMIN_OWNER_COMMANDS = OWNER_COMMANDS + (SUPERADMIN_COMMAND,)", COMMAND_MENU)

    def test_sync_receives_configured_superadmin_at_startup_and_after_setup(self):
        self.assertIn("superadmin_telegram_id=settings.superadmin_telegram_id", MAIN)
        self.assertIn("superadmin_telegram_id=settings.superadmin_telegram_id", HANDLERS)

    def test_group_registries_are_manual_only(self):
        tree = ast.parse(COMMAND_MENU)
        sync = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "sync_command_menus"
        )
        source = ast.get_source_segment(COMMAND_MENU, sync) or ""
        self.assertNotIn("GENERAL_OWNER_COMMANDS", source)
        self.assertNotIn("TOPIC_ADMIN_COMMANDS", source)

    def test_manual_group_handlers_still_exist(self):
        for command in (
            "setup", "stats", "set_period", "set_announcement", "set_timezone",
            "set_topic_template", "broadcast", "subscriber", "subscriber_history", "status",
        ):
            self.assertIn(f'Command("{command}")', HANDLERS)


if __name__ == "__main__":
    unittest.main()
