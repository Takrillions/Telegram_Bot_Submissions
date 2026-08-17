import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS_PATH = ROOT / "handlers.py"
HANDLERS = HANDLERS_PATH.read_text(encoding="utf-8")
TREE = ast.parse(HANDLERS)


def function_source(name: str) -> str:
    node = next(
        item for item in ast.walk(TREE)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(HANDLERS, node) or ""


class PreStartGlobalAuthorizationStaticTests(unittest.TestCase):
    def test_normal_owner_panel_has_no_global_profile_controls(self):
        keyboard = function_source("panel_keyboard")
        self.assertIn("show_superadmin_entry", keyboard)
        self.assertIn("if show_superadmin_entry", keyboard)
        self.assertIn('callback_data="sa:home"', keyboard)
        self.assertNotIn('callback_data="panel:prestart"', keyboard)

    def test_prestart_panel_callbacks_have_global_gate(self):
        panel = function_source("panel_callback")
        marker = 'data == "panel:prestart" or data.startswith("panel:prestart:")'
        self.assertIn(marker, panel)
        self.assertIn("GlobalAction.PRESTART_PROFILE", panel)
        self.assertIn("global_authorizer.require", panel)

    def test_prestart_fsm_and_save_callbacks_recheck_global_gate(self):
        actor_gate = function_source("prestart_actor_authorized")
        self.assertIn("GlobalAction.PRESTART_PROFILE", actor_gate)
        self.assertIn("global_authorizer.require", actor_gate)
        self.assertNotIn("ChannelAction.SETTINGS", actor_gate)

        description = function_source("prestart_description_input")
        media = function_source("prestart_media_input")
        confirm = function_source("prestart_card_confirm")
        self.assertIn("prestart_state_authorized", description)
        self.assertIn("prestart_state_authorized", media)
        self.assertIn("prestart_actor_authorized", confirm)


if __name__ == "__main__":
    unittest.main()
