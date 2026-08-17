from pathlib import Path
import tempfile
import unittest

from database import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_MIGRATIONS,
    Database,
    DraftNotEmptyError,
)
from templates import TEMPLATE_REGISTRY


class CustomHistoryStage8Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "bot.sqlite3")
        self.db = Database(self.path)
        await self.db.init()
        status, channel = await self.db.register_channel(
            owner_id=101,
            group_id=-10001,
            group_title="A",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Anon",
        )
        self.assertEqual(status, "created")
        self.channel_id = int(channel["channel_id"])

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _publish_text(self, text: str) -> int:
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="start.greeting",
            custom_text=text,
            updated_by=101,
        )
        return await self.db.publish_channel_custom_draft(
            channel_id=self.channel_id, published_by=101
        )

    async def test_schema_v27_adds_rollback_metadata(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 27)
        columns = await (await self.db.conn.execute(
            "PRAGMA table_info(channel_custom_drafts)"
        )).fetchall()
        names = {str(row["name"]) for row in columns}
        self.assertTrue({"publish_source", "publish_summary", "restore_revision_id"}.issubset(names))

    async def test_revision_history_is_ordered_and_diffable(self):
        first = await self._publish_text("FIRST {channel_name}")
        second = await self._publish_text("SECOND {channel_name}")
        rows = await self.db.list_channel_custom_revisions(
            channel_id=self.channel_id, limit=20
        )
        ids = [int(row["revision_id"]) for row in rows]
        self.assertGreaterEqual(len(ids), 3)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertIn(first, ids)
        self.assertIn(second, ids)
        diff = await self.db.diff_channel_custom_revision(
            channel_id=self.channel_id, revision_id=second
        )
        self.assertIn("template:start.greeting", diff["changed_keys"])

    async def test_restore_is_draft_only_then_publishes_new_rollback_revision(self):
        first = await self._publish_text("FIRST {channel_name}")
        second = await self._publish_text("SECOND {channel_name}")
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "SECOND {channel_name}",
        )

        staged = await self.db.stage_channel_custom_revision_restore(
            channel_id=self.channel_id, revision_id=first, restored_by=101
        )
        self.assertGreater(int(staged["staged"]), 0)
        # Restore never mutates live before explicit publish.
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "SECOND {channel_name}",
        )
        self.assertEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.channel_id, template_key="start.greeting"
            ),
            "FIRST {channel_name}",
        )
        draft = await self.db.get_channel_custom_draft_state(self.channel_id)
        self.assertEqual(str(draft["publish_source"]), "rollback")
        self.assertEqual(int(draft["restore_revision_id"]), first)

        rollback_revision = await self.db.publish_channel_custom_draft(
            channel_id=self.channel_id, published_by=101
        )
        self.assertNotIn(rollback_revision, {first, second})
        row = await self.db.get_channel_custom_revision(
            channel_id=self.channel_id, revision_id=rollback_revision
        )
        self.assertEqual(str(row["source"]), "rollback")
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "FIRST {channel_name}",
        )
        # Historical revisions remain intact.
        self.assertIsNotNone(await self.db.get_channel_custom_revision(
            channel_id=self.channel_id, revision_id=second
        ))
        audit = await self.db.list_customization_audit(
            channel_id=self.channel_id, scope_type="channel_custom", limit=100
        )
        actions = [str(row["action"]) for row in audit]
        self.assertIn("revision_restore_staged", actions)
        self.assertIn("draft_published", actions)

    async def test_existing_draft_blocks_restore_without_overwrite(self):
        first = await self._publish_text("FIRST {channel_name}")
        await self._publish_text("SECOND {channel_name}")
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="message.received",
            custom_text="MY UNSAVED DRAFT",
            updated_by=101,
        )
        with self.assertRaises(DraftNotEmptyError):
            await self.db.stage_channel_custom_revision_restore(
                channel_id=self.channel_id, revision_id=first, restored_by=101
            )
        self.assertEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.channel_id, template_key="message.received"
            ),
            "MY UNSAVED DRAFT",
        )

    async def test_cross_channel_revision_cannot_be_restored(self):
        first = await self._publish_text("FIRST {channel_name}")
        status, channel_b = await self.db.register_channel(
            owner_id=202,
            group_id=-10002,
            group_title="B",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="B",
        )
        self.assertEqual(status, "created")
        cid_b = int(channel_b["channel_id"])
        with self.assertRaises(ValueError):
            await self.db.stage_channel_custom_revision_restore(
                channel_id=cid_b, revision_id=first, restored_by=202
            )
        self.assertFalse(await self.db.has_channel_custom_draft(cid_b))

    async def test_audit_pagination_and_global_profile_actor(self):
        await self.db.set_bot_prestart_description(
            description="Global", updated_by=999
        )
        await self.db.set_bot_prestart_media(
            media_type="photo", media_file_id="file", updated_by=999
        )
        await self.db.remove_bot_prestart_media(updated_by=999)
        await self.db.reset_bot_prestart_card(updated_by=999)
        events = await self.db.list_customization_audit(
            scope_type="global_profile", limit=20
        )
        self.assertGreaterEqual(len(events), 4)
        self.assertTrue(all(
            row["actor_user_id"] is None or int(row["actor_user_id"]) == 999
            for row in events
        ))
        self.assertEqual(
            await self.db.count_customization_audit(scope_type="global_profile"),
            len(events),
        )
        page = await self.db.list_customization_audit(
            scope_type="global_profile", limit=2, offset=1
        )
        self.assertEqual(len(page), 2)

    async def test_v26_to_v27_preserves_channel_snapshot_and_extends_standard_only(self):
        await self.db.close()
        Path(self.path).unlink()
        old = Database(self.path, migrations=DEFAULT_MIGRATIONS[:26])
        await old.init()
        status, channel = await old.register_channel(
            owner_id=101,
            group_id=-10001,
            group_title="A",
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
        self.channel_id = cid
        state_after = await migrated.get_channel_custom_state(cid)
        self.assertEqual(int(state_after["active_revision_id"]), active_before)
        items_after = await migrated.get_channel_custom_items(
            channel_id=cid, revision_id=active_before,
            include_legacy_template_overlay=False,
        )
        self.assertEqual(items_after, items_before)
        standard = await migrated.get_standard_custom_items()
        self.assertIn("template:custom.history_overview", standard)
        self.assertIn("template:ui.panel.history", standard)
        await migrated.run_preflight()


class CustomHistoryStage8StaticTests(unittest.TestCase):
    def test_history_templates_are_channel_scoped(self):
        keys = (
            "custom.history_overview",
            "custom.history_revision",
            "custom.history_restore_prompt",
            "custom.audit_overview",
            "ui.panel.history",
            "ui.custom.restore",
        )
        for key in keys:
            self.assertIn(key, TEMPLATE_REGISTRY)
            self.assertEqual(TEMPLATE_REGISTRY[key].scope, "channel")

    def test_handlers_recheck_authorization_for_history_callbacks(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('F.data.startswith("custom:history:")', source)
        self.assertIn('F.data.startswith("custom:revision_restore_confirm:")', source)
        self.assertIn('action=ChannelAction.SETTINGS', source)
        self.assertIn('stage_channel_custom_revision_restore(', source)


if __name__ == "__main__":
    unittest.main()
