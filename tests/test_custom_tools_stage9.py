from pathlib import Path
import tempfile
import unittest

from database import CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS, Database, DraftNotEmptyError
from templates import TEMPLATE_REGISTRY


class CustomToolsStage9Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "bot.sqlite3")
        self.db = Database(self.path)
        await self.db.init()
        status, target = await self.db.register_channel(
            owner_id=101,
            group_id=-10001,
            group_title="Target",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Anon",
        )
        self.assertEqual(status, "created")
        self.target_id = int(target["channel_id"])

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _publish_target_text(self, text: str) -> int:
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            custom_text=text,
            updated_by=101,
        )
        return await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )

    async def test_schema_v28_adds_bulk_provenance(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 28)
        columns = await (await self.db.conn.execute(
            "PRAGMA table_info(channel_custom_drafts)"
        )).fetchall()
        names = {str(row["name"]) for row in columns}
        self.assertTrue({"source_channel_id", "source_standard_revision_id"}.issubset(names))

    async def test_reset_initial_is_staged_then_published_as_new_revision(self):
        initial_text = await self.db.get_channel_custom_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        await self._publish_target_text("CUSTOM {channel_name}")
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=self.target_id,
            media_type="photo",
            media_file_id="custom-photo",
            updated_by=101,
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )

        plan = await self.db.plan_channel_custom_initial_reset(channel_id=self.target_id)
        self.assertIn("template:start.greeting", plan["changed_keys"])
        self.assertIn("start_card.media", plan["changed_keys"])
        staged = await self.db.stage_channel_custom_initial_reset(
            channel_id=self.target_id, reset_by=101
        )
        self.assertGreaterEqual(int(staged["staged"]), 2)
        # Live is unchanged before publish.
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "CUSTOM {channel_name}",
        )
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(self.target_id),
            {"media_type": "photo", "media_file_id": "custom-photo"},
        )
        draft = await self.db.get_channel_custom_draft_state(self.target_id)
        self.assertEqual(str(draft["publish_source"]), "reset_initial")

        revision_id = await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        revision = await self.db.get_channel_custom_revision(
            channel_id=self.target_id, revision_id=revision_id
        )
        self.assertEqual(str(revision["source"]), "reset_initial")
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            initial_text,
        )
        self.assertIsNone(await self.db.get_channel_custom_start_card_media(self.target_id))

    async def test_apply_current_standard_is_draft_only_and_tracks_standard_revision(self):
        await self._publish_target_text("CUSTOM {channel_name}")
        standard_state = await self.db.get_standard_custom_state()
        standard_revision_id = int(standard_state["active_revision_id"])
        standard_items = await self.db.get_standard_custom_items()
        expected = standard_items["template:start.greeting"]["payload"]["text"]

        plan = await self.db.plan_channel_custom_apply_current_standard(
            channel_id=self.target_id
        )
        self.assertEqual(int(plan["source_standard_revision_id"]), standard_revision_id)
        self.assertIn("template:start.greeting", plan["changed_keys"])
        await self.db.stage_channel_custom_current_standard(
            channel_id=self.target_id, applied_by=101
        )
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "CUSTOM {channel_name}",
        )
        draft = await self.db.get_channel_custom_draft_state(self.target_id)
        self.assertEqual(str(draft["publish_source"]), "apply_current_standard")
        self.assertEqual(int(draft["source_standard_revision_id"]), standard_revision_id)

        revision_id = await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        revision = await self.db.get_channel_custom_revision(
            channel_id=self.target_id, revision_id=revision_id
        )
        self.assertEqual(str(revision["source"]), "apply_current_standard")
        self.assertEqual(int(revision["source_standard_revision_id"]), standard_revision_id)
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            expected,
        )

    async def test_copy_between_own_channels_is_isolated_and_published_explicitly(self):
        status, source = await self.db.register_channel(
            owner_id=101,
            group_id=-10002,
            group_title="Source",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Src",
        )
        self.assertEqual(status, "created")
        source_id = int(source["channel_id"])
        await self.db.set_channel_custom_draft_template_text(
            channel_id=source_id,
            template_key="start.greeting",
            custom_text="SOURCE {channel_name}",
            updated_by=101,
        )
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=source_id,
            media_type="animation",
            media_file_id="source-gif",
            updated_by=101,
        )
        await self.db.publish_channel_custom_draft(channel_id=source_id, published_by=101)

        target_before = await self.db.get_channel_custom_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        plan = await self.db.plan_channel_custom_copy(
            channel_id=self.target_id, source_channel_id=source_id, actor_id=101
        )
        self.assertEqual(int(plan["source_channel_id"]), source_id)
        self.assertIn("template:start.greeting", plan["changed_keys"])
        self.assertIn("start_card.media", plan["changed_keys"])
        await self.db.stage_channel_custom_copy(
            channel_id=self.target_id, source_channel_id=source_id, copied_by=101
        )
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            target_before,
        )
        draft = await self.db.get_channel_custom_draft_state(self.target_id)
        self.assertEqual(str(draft["publish_source"]), "copy_from_channel")
        self.assertEqual(int(draft["source_channel_id"]), source_id)

        revision_id = await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        revision = await self.db.get_channel_custom_revision(
            channel_id=self.target_id, revision_id=revision_id
        )
        self.assertEqual(str(revision["source"]), "copy_from_channel")
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "SOURCE {channel_name}",
        )
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(self.target_id),
            {"media_type": "animation", "media_file_id": "source-gif"},
        )
        # Source remains untouched.
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=source_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "SOURCE {channel_name}",
        )

    async def test_foreign_owner_copy_is_denied_and_creates_no_draft(self):
        status, foreign = await self.db.register_channel(
            owner_id=202,
            group_id=-10003,
            group_title="Foreign",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="F",
        )
        self.assertEqual(status, "created")
        foreign_id = int(foreign["channel_id"])
        with self.assertRaises(PermissionError):
            await self.db.plan_channel_custom_copy(
                channel_id=self.target_id, source_channel_id=foreign_id, actor_id=101
            )
        with self.assertRaises(PermissionError):
            await self.db.stage_channel_custom_copy(
                channel_id=self.target_id, source_channel_id=foreign_id, copied_by=101
            )
        self.assertFalse(await self.db.has_channel_custom_draft(self.target_id))

    async def test_existing_draft_blocks_all_bulk_tools(self):
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.target_id,
            template_key="message.received",
            custom_text="UNSAVED",
            updated_by=101,
        )
        with self.assertRaises(DraftNotEmptyError):
            await self.db.stage_channel_custom_current_standard(
                channel_id=self.target_id, applied_by=101
            )
        with self.assertRaises(DraftNotEmptyError):
            await self.db.stage_channel_custom_initial_reset(
                channel_id=self.target_id, reset_by=101
            )
        self.assertEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.target_id, template_key="message.received"
            ),
            "UNSAVED",
        )

    async def test_copy_publish_revalidates_source_ownership(self):
        status, source = await self.db.register_channel(
            owner_id=101,
            group_id=-10004,
            group_title="Source",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Src",
        )
        source_id = int(source["channel_id"])
        await self.db.set_channel_custom_draft_template_text(
            channel_id=source_id,
            template_key="start.greeting",
            custom_text="SOURCE {channel_name}",
            updated_by=101,
        )
        await self.db.publish_channel_custom_draft(channel_id=source_id, published_by=101)
        await self.db.stage_channel_custom_copy(
            channel_id=self.target_id, source_channel_id=source_id, copied_by=101
        )
        # Simulate a corrupted/forged draft source after staging.
        status, foreign = await self.db.register_channel(
            owner_id=202,
            group_id=-10005,
            group_title="Foreign",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="F",
        )
        foreign_id = int(foreign["channel_id"])
        await self.db.conn.execute(
            "UPDATE channel_custom_drafts SET source_channel_id=? WHERE channel_id=?",
            (foreign_id, self.target_id),
        )
        await self.db.conn.commit()
        with self.assertRaises(PermissionError):
            await self.db.publish_channel_custom_draft(
                channel_id=self.target_id, published_by=101
            )
        self.assertTrue(await self.db.has_channel_custom_draft(self.target_id))

    async def test_v27_to_v28_preserves_live_channel_revision_and_extends_standard(self):
        await self.db.close()
        Path(self.path).unlink()
        old = Database(self.path, migrations=DEFAULT_MIGRATIONS[:27])
        await old.init()
        status, channel = await old.register_channel(
            owner_id=101,
            group_id=-10001,
            group_title="Target",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Anon",
        )
        self.assertEqual(status, "created")
        cid = int(channel["channel_id"])
        state_before = await old.get_channel_custom_state(cid)
        active_before = int(state_before["active_revision_id"])
        items_before = await old.get_channel_custom_items(
            channel_id=cid, revision_id=active_before,
            include_legacy_template_overlay=False,
        )
        await old.close()

        migrated = Database(self.path)
        await migrated.init()
        self.db = migrated
        self.target_id = cid
        state_after = await migrated.get_channel_custom_state(cid)
        self.assertEqual(int(state_after["active_revision_id"]), active_before)
        items_after = await migrated.get_channel_custom_items(
            channel_id=cid, revision_id=active_before,
            include_legacy_template_overlay=False,
        )
        self.assertEqual(items_after, items_before)
        standard = await migrated.get_standard_custom_items()
        self.assertIn("template:custom.tools_overview", standard)
        self.assertIn("template:ui.panel.custom_tools", standard)
        await migrated.run_preflight()


class CustomToolsStage9StaticTests(unittest.TestCase):
    def test_stage9_templates_are_channel_scoped(self):
        for key in (
            "custom.tools_overview",
            "custom.tools_plan",
            "custom.tools_copy_prompt",
            "ui.panel.custom_tools",
            "ui.custom.reset_initial",
            "ui.custom.apply_standard",
            "ui.custom.copy_from_channel",
        ):
            self.assertIn(key, TEMPLATE_REGISTRY)
            self.assertEqual(TEMPLATE_REGISTRY[key].scope, "channel")

    def test_handlers_have_bulk_tools_and_owner_copy_guard(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('callback_data="panel:custom_tools"', source)
        self.assertIn("plan_channel_custom_initial_reset", source)
        self.assertIn("stage_channel_custom_current_standard", source)
        self.assertIn("stage_channel_custom_copy", source)
        self.assertIn("list_enabled_channels_for_owner(callback.from_user.id)", source)

    def test_database_registers_v28_and_publish_sources(self):
        source = Path("database.py").read_text(encoding="utf-8")
        self.assertIn('Migration(28, "custom_tools_and_provenance", apply_custom_tools_v28)', source)
        self.assertIn('"reset_initial"', source)
        self.assertIn('"apply_current_standard"', source)
        self.assertIn('"copy_from_channel"', source)
        self.assertIn("Copy draft no longer belongs to the same owner", source)


if __name__ == "__main__":
    unittest.main()
