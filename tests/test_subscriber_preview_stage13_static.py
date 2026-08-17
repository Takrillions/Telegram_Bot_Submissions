import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = (ROOT / "handlers.py").read_text(encoding="utf-8")
DATABASE = (ROOT / "database.py").read_text(encoding="utf-8")
PREVIEW = (ROOT / "subscriber_preview.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "subscriber-preview.md").read_text(encoding="utf-8")


def function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found")


class SubscriberPreviewStage13StaticTests(unittest.TestCase):
    def test_panel_entry_and_private_owner_gate_exist(self):
        self.assertIn("Посмотреть глазами подписчика", HANDLERS)
        self.assertIn("preview:subscriber:home:", HANDLERS)
        callback = function_source(HANDLERS, "subscriber_preview_callback")
        self.assertIn("ChatType.PRIVATE", callback)
        self.assertIn("action=ChannelAction.SETTINGS", callback)

    def test_preview_privacy_buttons_cannot_trigger_real_privacy_callback(self):
        sender = function_source(HANDLERS, "send_subscriber_preview_scenario")
        self.assertIn("preview:subscriber:noop:", sender)
        self.assertNotIn('callback_data="privacy:', sender)

    def test_preview_functions_do_not_call_runtime_mutations(self):
        source = "\n".join(
            function_source(HANDLERS, name)
            for name in (
                "send_subscriber_preview_scenario",
                "send_all_subscriber_preview_scenarios",
                "subscriber_preview_callback",
            )
        )
        forbidden = (
            "set_active_channel",
            "set_privacy_mode",
            "ensure_anonymous_tag",
            "register_subscriber",
            "record_message_event",
            "mark_topic_answered",
            "touch_topic",
            "create_forum_topic",
            "create_topic_mapping",
            "apply_subscriber_sanction",
            "set_user_blocked",
        )
        for name in forbidden:
            self.assertNotIn(name, source)

    def test_message_received_preview_matches_real_success_path(self):
        runtime = function_source(HANDLERS, "_flush_user_messages_locked")
        self.assertIn('"message.received"', runtime)
        self.assertIn("record_message_event", runtime)

    def test_draft_overlay_is_explicit(self):
        sender = function_source(HANDLERS, "send_subscriber_preview_scenario")
        self.assertIn("include_draft=True", sender)
        renderer = function_source(PREVIEW, "render_subscriber_preview_scenario")
        self.assertIn("include_draft=True", renderer)

    def test_start_card_preview_has_non_customizable_marker(self):
        source = function_source(HANDLERS, "send_channel_start_card_preview")
        self.assertIn("_subscriber_preview_marker", source)
        self.assertNotIn('"start_card.preview_header"', source)

    def test_main_customization_surfaces_show_context_header(self):
        for name in (
            "_panel_text",
            "custom_history_view",
            "custom_revision_view",
            "custom_audit_view",
            "custom_tools_view",
            "custom_transfer_view",
        ):
            self.assertIn("customization_context_text", function_source(HANDLERS, name))
        self.assertGreaterEqual(HANDLERS.count("customization_context_text(db=db, channel=channel)"), 8)

    def test_no_stage13_database_migration(self):
        self.assertIn('Migration(30, "standard_global_separation"', DATABASE)
        self.assertNotIn("Migration(31,", DATABASE)

    def test_documentation_states_side_effect_guarantees(self):
        self.assertIn("Посмотреть глазами подписчика", README)
        for phrase in (
            "не меняет active subscriber channel",
            "не вызывает message-event analytics",
            "не создаёт forum topic",
        ):
            self.assertIn(phrase, DOC)

    def test_callback_payloads_are_well_below_telegram_limit_for_normal_channel_ids(self):
        # channel_id is the local integer PK, not the -100... Telegram group id.
        examples = [
            "preview:subscriber:home:2147483647",
            "preview:subscriber:all:2147483647",
            "preview:subscriber:noop:2147483647",
            "preview:subscriber:scenario:unavailable:2147483647",
        ]
        self.assertTrue(all(len(item.encode("utf-8")) <= 64 for item in examples))


if __name__ == "__main__":
    unittest.main()
