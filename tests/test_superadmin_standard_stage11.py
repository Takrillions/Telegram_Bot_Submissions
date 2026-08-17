import ast
import tempfile
import unittest
from pathlib import Path

from authorization import GlobalAction, GlobalAuthorizer
from database import CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS, Database
from templates import TEMPLATE_REGISTRY

ROOT = Path(__file__).resolve().parents[1]


class SuperadminStage11StaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = (ROOT / "handlers.py").read_text(encoding="utf-8")
        cls.database = (ROOT / "database.py").read_text(encoding="utf-8")
        cls.authorization = (ROOT / "authorization.py").read_text(encoding="utf-8")
        ast.parse(cls.handlers)
        ast.parse(cls.database)
        ast.parse(cls.authorization)

    def test_global_actions_include_standard_pack_and_panel(self):
        self.assertIn('SUPERADMIN_PANEL = "superadmin_panel"', self.authorization)
        self.assertIn('STANDARD_PACK = "standard_pack"', self.authorization)

    def test_separate_superadmin_private_surface_exists(self):
        self.assertIn('@router.message(Command("superadmin"))', self.handlers)
        self.assertIn('F.data.startswith("sa:")', self.handlers)
        self.assertIn('callback_data="sa:std"', self.handlers)
        self.assertIn('callback_data="sa:profile"', self.handlers)
        self.assertIn('callback_data="sa:audit:0"', self.handlers)

    def test_owner_panel_has_no_global_profile_editor(self):
        start = self.handlers.index("async def panel_keyboard")
        end = self.handlers.index("async def cleanup_keyboard", start)
        panel = self.handlers[start:end]
        self.assertNotIn('panel:prestart', panel)
        self.assertIn('callback_data="sa:home"', panel)

    def test_global_profile_fsm_has_no_channel_authorization_dependency(self):
        start = self.handlers.index("async def prestart_actor_authorized")
        end = self.handlers.index("# Standard Custom Pack editor FSM", start)
        body = self.handlers[start:end]
        self.assertIn("GlobalAction.PRESTART_PROFILE", body)
        self.assertNotIn("ChannelAction.SETTINGS", body)
        self.assertNotIn("prestart_auth_channel_id", body)

    def test_superadmin_callback_routes_to_specific_global_action(self):
        start = self.handlers.index("async def superadmin_callback")
        end = self.handlers.index('@router.message(Command("panel"))', start)
        body = self.handlers[start:end]
        self.assertIn('required_action = GlobalAction.PRESTART_PROFILE', body)
        self.assertIn('required_action = GlobalAction.STANDARD_PACK', body)
        self.assertIn('action=required_action', body)

    def test_standard_editor_uses_immutable_revision_api(self):
        self.assertIn("publish_standard_custom_template_text", self.handlers)
        self.assertIn("set_standard_custom_start_card_media", self.handlers)
        self.assertIn("list_standard_custom_revisions", self.handlers)
        self.assertIn("global_standard_changed", self.database)

    def test_superadmin_callback_payloads_fit_telegram_limit(self):
        import re

        literals = re.findall(r'callback_data=["\'](sa:[^"\']+)["\']', self.handlers)
        self.assertTrue(literals)
        for payload in literals:
            self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)
        for spec in TEMPLATE_REGISTRY.values():
            if spec.scope != "channel":
                continue
            for variable in spec.variables:
                payload = f"sa:field:{variable}"
                self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)

    def test_superadmin_navigation_clears_stale_editor_state(self):
        for marker in (
            'if data == "sa:profile":',
            'if data == "sa:std":',
            'if data == "sa:std:start":',
            'if len(parts) == 4 and parts[:3] == ["sa", "std", "hist"]:',
            'if len(parts) == 3 and parts[:2] == ["sa", "audit"]:',
        ):
            start = self.handlers.index(marker)
            body = self.handlers[start:start + 350]
            self.assertIn("await state.clear()", body, marker)

    def test_v30_migration_separates_global_templates_from_active_standard(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 30)
        self.assertIn('Migration(30, "standard_global_separation", apply_standard_global_separation_v30)', self.database)
        body = self.database[
            self.database.index("async def apply_standard_global_separation_v30"):
            self.database.index("async def apply_anonymous_cycle_state")
        ]
        self.assertIn('spec.scope == "channel"', body)
        self.assertIn('item_key == "start_card.media"', body)
        self.assertNotIn("UPDATE channel_custom_state", body)


class SuperadminStage11AuthorizationTests(unittest.TestCase):
    def test_standard_pack_requires_exact_superadmin(self):
        auth = GlobalAuthorizer(superadmin_telegram_id=123456)
        self.assertTrue(auth.require(actor_id=123456, action=GlobalAction.STANDARD_PACK).allowed)
        self.assertFalse(auth.require(actor_id=654321, action=GlobalAction.STANDARD_PACK).allowed)
        self.assertFalse(GlobalAuthorizer(superadmin_telegram_id=None).require(
            actor_id=123456, action=GlobalAction.STANDARD_PACK
        ).allowed)


class SuperadminStage11DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "bot.sqlite3")
        self.db = Database(self.db_path, backup_dir=Path(self.tmp.name) / "backups")
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _register(self, owner_id: int, group_id: int, title: str):
        status, channel = await self.db.register_channel(
            owner_id=owner_id,
            group_id=group_id,
            group_title=title,
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Анон",
        )
        self.assertEqual(status, "created")
        self.assertIsNotNone(channel)
        return channel

    async def test_active_standard_contains_only_channel_scoped_templates(self):
        items = await self.db.get_standard_custom_items()
        self.assertTrue(items)
        self.assertNotIn("template:prestart.overview", items)
        self.assertNotIn("template:setup.supergroup_required", items)
        for item_key, item in items.items():
            if item_key == "start_card.media":
                continue
            self.assertTrue(item_key.startswith("template:"))
            key = item_key[len("template:"):]
            self.assertIn(key, TEMPLATE_REGISTRY)
            self.assertEqual(TEMPLATE_REGISTRY[key].scope, "channel")

    async def test_standard_edit_does_not_change_existing_channel_but_new_setup_gets_it(self):
        channel_a = await self._register(1001, -100001, "A")
        channel_a_id = int(channel_a["channel_id"])
        before = await self.db.get_channel_custom_template_text(
            channel_id=channel_a_id, template_key="start.greeting", include_legacy_template_overlay=False
        )
        self.assertIsNotNone(before)

        result = await self.db.publish_standard_custom_template_text(
            template_key="start.greeting",
            custom_text="Новый стандарт: {channel_name}",
            updated_by=777,
        )
        self.assertTrue(result["changed"])
        self.assertGreater(int(result["revision_id"]), int(result["previous_revision_id"]))

        still_a = await self.db.get_channel_custom_template_text(
            channel_id=channel_a_id, template_key="start.greeting", include_legacy_template_overlay=False
        )
        self.assertEqual(still_a, before)

        channel_b = await self._register(1002, -100002, "B")
        channel_b_id = int(channel_b["channel_id"])
        new_b = await self.db.get_channel_custom_template_text(
            channel_id=channel_b_id, template_key="start.greeting", include_legacy_template_overlay=False
        )
        self.assertEqual(new_b, "Новый стандарт: {channel_name}")

    async def test_standard_media_is_versioned_and_snapshotted_only_to_new_channel(self):
        a = await self._register(2001, -100011, "No media yet")
        a_id = int(a["channel_id"])
        self.assertIsNone(await self.db.get_channel_custom_start_card_media(a_id))

        result = await self.db.set_standard_custom_start_card_media(
            media_type="photo", media_file_id="standard-photo-id", updated_by=777
        )
        self.assertTrue(result["changed"])
        media = await self.db.get_standard_custom_start_card_media()
        self.assertEqual(media, {"media_type": "photo", "media_file_id": "standard-photo-id"})
        self.assertIsNone(await self.db.get_channel_custom_start_card_media(a_id))

        b = await self._register(2002, -100012, "Gets media")
        b_id = int(b["channel_id"])
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(b_id),
            {"media_type": "photo", "media_file_id": "standard-photo-id"},
        )

    async def test_global_template_cannot_be_written_into_standard_editor_api(self):
        with self.assertRaises(ValueError):
            await self.db.publish_standard_custom_template_text(
                template_key="prestart.overview",
                custom_text=TEMPLATE_REGISTRY["prestart.overview"].default,
                updated_by=777,
            )

    async def test_standard_edits_create_global_audit_and_revision_history(self):
        before = await self.db.count_standard_custom_revisions()
        result = await self.db.publish_standard_custom_template_text(
            template_key="start.greeting",
            custom_text="Audit standard {channel_name}",
            updated_by=777,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(await self.db.count_standard_custom_revisions(), before + 1)
        audit = await self.db.list_customization_audit(scope_type="global_standard", limit=20)
        event = next(row for row in audit if str(row["action"]) == "global_standard_changed")
        self.assertEqual(int(event["actor_user_id"]), 777)
        self.assertEqual(str(event["target_key"]), "template:start.greeting")


    async def test_v29_to_v30_preserves_existing_channel_snapshot(self):
        await self.db.close()
        legacy_path = str(Path(self.tmp.name) / "v29.sqlite3")
        v29 = Database(legacy_path, migrations=DEFAULT_MIGRATIONS[:29], backup_dir=Path(self.tmp.name) / "v29_backups")
        await v29.init()
        status, channel = await v29.register_channel(
            owner_id=3001, group_id=-100021, group_title="Legacy v29",
            default_reset_days=30, default_notice_text="notice",
            default_timezone="Asia/Tashkent", anonymous_prefix="Анон",
        )
        self.assertEqual(status, "created")
        channel_id = int(channel["channel_id"])
        before_state = await v29.get_channel_custom_state(channel_id)
        before_revision = int(before_state["active_revision_id"])
        before_items = await v29.get_channel_custom_items(
            channel_id=channel_id, revision_id=before_revision, include_legacy_template_overlay=False
        )
        self.assertIn("template:prestart.overview", before_items)
        await v29.close()

        upgraded = Database(legacy_path, backup_dir=Path(self.tmp.name) / "v30_backups")
        await upgraded.init()
        try:
            after_state = await upgraded.get_channel_custom_state(channel_id)
            self.assertEqual(int(after_state["active_revision_id"]), before_revision)
            after_items = await upgraded.get_channel_custom_items(
                channel_id=channel_id, revision_id=before_revision, include_legacy_template_overlay=False
            )
            self.assertEqual(after_items, before_items)
            active_standard = await upgraded.get_standard_custom_items()
            self.assertNotIn("template:prestart.overview", active_standard)
            integrity = await (await upgraded.conn.execute("PRAGMA integrity_check")).fetchone()
            foreign = await (await upgraded.conn.execute("PRAGMA foreign_key_check")).fetchall()
            self.assertEqual(str(integrity[0]), "ok")
            self.assertEqual(foreign, [])
        finally:
            await upgraded.close()
        self.db = Database(self.db_path, backup_dir=Path(self.tmp.name) / "backups")
        await self.db.init()

    async def test_integrity_after_stage11_operations(self):
        await self.db.publish_standard_custom_template_text(
            template_key="start.greeting", custom_text="Integrity {channel_name}", updated_by=777
        )
        integrity = await (await self.db.conn.execute("PRAGMA integrity_check")).fetchone()
        foreign = await (await self.db.conn.execute("PRAGMA foreign_key_check")).fetchall()
        self.assertEqual(str(integrity[0]), "ok")
        self.assertEqual(foreign, [])


if __name__ == "__main__":
    unittest.main()
