import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChannelStartCardStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = (ROOT / "handlers.py").read_text(encoding="utf-8")
        cls.database = (ROOT / "database.py").read_text(encoding="utf-8")
        cls.templates = (ROOT / "templates.py").read_text(encoding="utf-8")
        ast.parse(cls.handlers)
        ast.parse(cls.database)
        ast.parse(cls.templates)

    def test_schema_has_channel_scoped_media_table(self):
        self.assertIn('Migration(24, "channel_start_card_media", apply_channel_start_card_media)', self.database)
        self.assertIn("CREATE TABLE channel_start_card_media", self.database)
        self.assertIn("channel_id INTEGER PRIMARY KEY", self.database)
        self.assertIn("FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE", self.database)

    def test_v24_extends_standard_with_successor_revision_without_touching_channels(self):
        self.assertIn("schema_v24_defaults", self.database)
        self.assertIn("schema_defaults_added", self.database)
        self.assertIn("previous_revision_id", self.database)
        self.assertNotIn("UPDATE channel_custom_state SET", self.database[self.database.index("async def apply_channel_start_card_media"):self.database.index("async def apply_template_surface_v25")])

    def test_media_api_requires_channel_id_and_audits_changes(self):
        self.assertIn("async def get_channel_start_card_media(self, channel_id: int)", self.database)
        self.assertIn("async def set_channel_start_card_media", self.database)
        self.assertIn("async def remove_channel_start_card_media", self.database)
        self.assertIn("channel_start_card_media_set", self.database)
        self.assertIn("channel_start_card_media_removed", self.database)

    def test_owner_panel_exposes_channel_card_and_only_links_superadmin_to_separate_global_section(self):
        self.assertIn('"ui.panel.start_card"', self.handlers)
        self.assertIn('callback_data="panel:start_card"', self.handlers)
        self.assertNotIn('text="Оформление бота (глобально)", callback_data="panel:prestart"', self.handlers)
        self.assertIn('text="Глобальное управление ботом", callback_data="sa:home"', self.handlers)
        self.assertIn("show_superadmin_entry=global_authorizer.is_superadmin(actor_id)", self.handlers)

    def test_deep_link_uses_channel_specific_card(self):
        start_pos = self.handlers.index("async def start_handler")
        privacy_pos = self.handlers.index("@router.message(Command(\"channels\")", start_pos)
        body = self.handlers[start_pos:privacy_pos]
        self.assertIn("await db.attach_subscriber", body)
        self.assertIn("await send_channel_start_card(message=message, db=db, channel=channel)", body)
        self.assertNotIn('"prestart.', body)

    def test_card_sends_media_then_text_and_falls_back(self):
        start = self.handlers.index("async def send_channel_start_card(")
        end = self.handlers.index("async def send_channel_start_card_preview(", start)
        body = self.handlers[start:end]
        media_positions = [body.index("await message.answer_photo"), body.index("await message.answer_video"), body.index("await message.answer_animation")]
        text_pos = body.index("await message.answer(text)")
        self.assertTrue(all(pos < text_pos for pos in media_positions))
        self.assertIn("except TelegramAPIError", body)
        self.assertIn("media_ok = False", body)

    def test_channel_editor_uses_channel_owner_authorization(self):
        self.assertIn("ChannelStartCardFlow", self.handlers)
        self.assertIn('action=ChannelAction.SETTINGS', self.handlers)
        self.assertIn('template_key="start.greeting"', self.handlers)
        self.assertIn("set_channel_custom_draft_start_card_media", self.handlers)

    def test_runtime_ui_explains_channel_scope(self):
        self.assertIn('"start_card.overview"', self.templates)
        self.assertIn("не меняет глобальный профиль бота", self.templates)
        self.assertIn('"start_card.media_stale"', self.templates)


if __name__ == "__main__":
    unittest.main()
