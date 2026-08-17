from pathlib import Path
import tempfile
import unittest

from database import Database, DraftConflictError
from templates import render_template


class FakeDraftDB:
    def __init__(self):
        self.live = {(1, "start.greeting"): "LIVE {channel_name}"}
        self.draft = {(1, "start.greeting"): "DRAFT {channel_name}"}

    async def get_channel_custom_template_text(
        self, *, channel_id: int, template_key: str,
        include_legacy_template_overlay: bool = True,
        revision_id=None, include_draft: bool = False,
    ):
        if include_draft and (channel_id, template_key) in self.draft:
            return self.draft[(channel_id, template_key)]
        return self.live.get((channel_id, template_key))

    async def get_template_override(self, *, channel_id: int, template_key: str):
        return None


class DraftRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_render_never_reads_unpublished_draft(self):
        db = FakeDraftDB()
        rendered = await render_template(db, 1, "start.greeting", channel_name="A")
        self.assertEqual(rendered, "LIVE A")

    async def test_preview_can_opt_into_draft_overlay(self):
        db = FakeDraftDB()
        rendered = await render_template(
            db, 1, "start.greeting", include_draft=True, channel_name="A"
        )
        self.assertEqual(rendered, "DRAFT A")


class DraftDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "bot.sqlite3"))
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

    async def test_template_write_is_draft_only_until_publish(self):
        live_before = await self.db.get_channel_custom_template_text(
            channel_id=self.channel_id, template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="start.greeting",
            custom_text="DRAFT {channel_name}",
            updated_by=101,
        )
        live_after = await self.db.get_channel_custom_template_text(
            channel_id=self.channel_id, template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        draft = await self.db.get_channel_custom_draft_template_text(
            channel_id=self.channel_id, template_key="start.greeting"
        )
        self.assertEqual(live_after, live_before)
        self.assertEqual(draft, "DRAFT {channel_name}")
        self.assertEqual(await self.db.get_channel_custom_draft_count(self.channel_id), 1)

    async def test_multi_item_publish_is_atomic_revision(self):
        state_before = await self.db.get_channel_custom_state(self.channel_id)
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="start.greeting",
            custom_text="PUBLISHED {channel_name}",
            updated_by=101,
        )
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=self.channel_id,
            media_type="photo",
            media_file_id="file-1",
            updated_by=101,
        )
        revision_id = await self.db.publish_channel_custom_draft(
            channel_id=self.channel_id, published_by=101
        )
        self.assertNotEqual(revision_id, int(state_before["active_revision_id"]))
        state_after = await self.db.get_channel_custom_state(self.channel_id)
        self.assertEqual(int(state_after["active_revision_id"]), revision_id)
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id, template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "PUBLISHED {channel_name}",
        )
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(self.channel_id),
            {"media_type": "photo", "media_file_id": "file-1"},
        )
        self.assertFalse(await self.db.has_channel_custom_draft(self.channel_id))
        audit = await self.db.list_customization_audit(channel_id=self.channel_id)
        self.assertTrue(any(str(row["action"]) == "draft_published" for row in audit))

    async def test_discard_leaves_live_revision_untouched(self):
        state_before = await self.db.get_channel_custom_state(self.channel_id)
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="start.greeting",
            custom_text="THROWAWAY {channel_name}",
            updated_by=101,
        )
        self.assertTrue(await self.db.discard_channel_custom_draft(
            channel_id=self.channel_id, discarded_by=101
        ))
        state_after = await self.db.get_channel_custom_state(self.channel_id)
        self.assertEqual(int(state_before["active_revision_id"]), int(state_after["active_revision_id"]))
        self.assertFalse(await self.db.has_channel_custom_draft(self.channel_id))


    async def test_v26_migration_preserves_preexisting_override_and_media(self):
        await self.db.close()
        # Recreate a database stopped at v25, then seed the exact mutable state
        # that Stage 7 must consolidate.
        import os
        os.remove(self.db.path)
        from database import DEFAULT_MIGRATIONS
        old = Database(self.db.path, migrations=DEFAULT_MIGRATIONS[:25])
        await old.init()
        status, channel = await old.register_channel(
            owner_id=101, group_id=-10001, group_title="A",
            default_reset_days=30, default_notice_text="notice",
            default_timezone="Asia/Tashkent", anonymous_prefix="Anon",
        )
        self.assertEqual(status, "created")
        cid = int(channel["channel_id"])
        await old.set_template_override(
            channel_id=cid, template_key="start.greeting",
            custom_text="LEGACY {channel_name}", updated_by=101,
        )
        await old.set_channel_start_card_media(
            channel_id=cid, media_type="photo", media_file_id="legacy-file", updated_by=101,
        )
        await old.close()

        migrated = Database(self.db.path)
        await migrated.init()
        self.db = migrated
        self.channel_id = cid
        self.assertEqual(
            await migrated.get_channel_custom_template_text(
                channel_id=cid, template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "LEGACY {channel_name}",
        )
        self.assertEqual(
            await migrated.get_channel_custom_start_card_media(cid),
            {"media_type": "photo", "media_file_id": "legacy-file"},
        )
        self.assertIsNone(await migrated.get_template_override(
            channel_id=cid, template_key="start.greeting"
        ))
        await migrated.run_preflight()

    async def test_failed_publish_rolls_back_revision_pointer_and_keeps_draft(self):
        state_before = await self.db.get_channel_custom_state(self.channel_id)
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=self.channel_id, media_type="photo",
            media_file_id="will-break", updated_by=101,
        )
        # Deliberately corrupt the media payload after the valid draft write.
        await self.db.conn.execute(
            "UPDATE channel_custom_draft_items SET payload_json='{}' WHERE channel_id=? AND item_key='start_card.media'",
            (self.channel_id,),
        )
        await self.db.conn.commit()
        with self.assertRaises(ValueError):
            await self.db.publish_channel_custom_draft(
                channel_id=self.channel_id, published_by=101
            )
        state_after = await self.db.get_channel_custom_state(self.channel_id)
        self.assertEqual(int(state_before["active_revision_id"]), int(state_after["active_revision_id"]))
        self.assertTrue(await self.db.has_channel_custom_draft(self.channel_id))


    async def test_drafts_are_strictly_channel_scoped(self):
        status, channel_b = await self.db.register_channel(
            owner_id=202, group_id=-10002, group_title="B",
            default_reset_days=30, default_notice_text="notice",
            default_timezone="Asia/Tashkent", anonymous_prefix="B",
        )
        self.assertEqual(status, "created")
        cid_b = int(channel_b["channel_id"])
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id, template_key="start.greeting",
            custom_text="ONLY A {channel_name}", updated_by=101,
        )
        self.assertEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.channel_id, template_key="start.greeting"
            ),
            "ONLY A {channel_name}",
        )
        self.assertIsNone(await self.db.get_channel_custom_draft_template_text(
            channel_id=cid_b, template_key="start.greeting"
        ))
        live_b = await self.db.get_channel_custom_template_text(
            channel_id=cid_b, template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.channel_id, published_by=101
        )
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=cid_b, template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            live_b,
        )

    async def test_reset_is_staged_not_immediately_live(self):
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id, template_key="start.greeting",
            custom_text="CUSTOM {channel_name}", updated_by=101,
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.channel_id, published_by=101
        )
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id, template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "CUSTOM {channel_name}",
        )
        await self.db.stage_channel_custom_template_reset(
            channel_id=self.channel_id, template_key="start.greeting", updated_by=101
        )
        # Live stays custom until explicit publish.
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.channel_id, template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "CUSTOM {channel_name}",
        )
        self.assertNotEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.channel_id, template_key="start.greeting"
            ),
            "CUSTOM {channel_name}",
        )

    async def test_stale_base_revision_fails_closed(self):
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.channel_id,
            template_key="start.greeting",
            custom_text="STALE {channel_name}",
            updated_by=101,
        )
        # Simulate another publication changing the active pointer after this draft.
        state = await self.db.get_channel_custom_state(self.channel_id)
        items = await self.db.get_channel_custom_items(
            channel_id=self.channel_id, include_legacy_template_overlay=False
        )
        now = "2026-08-17T00:00:00+00:00"
        cur = await self.db.conn.execute(
            "INSERT INTO channel_custom_revisions(channel_id,source,source_standard_revision_id,created_at,created_by,summary) VALUES(?,?,?,?,?,?)",
            (self.channel_id, "test", state["source_standard_revision_id"], now, 999, "test"),
        )
        other_revision = int(cur.lastrowid)
        for key, item in items.items():
            import json
            await self.db.conn.execute(
                "INSERT INTO channel_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                (other_revision, key, item["item_type"], json.dumps(item["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        await self.db.conn.execute(
            "UPDATE channel_custom_state SET active_revision_id=? WHERE channel_id=?",
            (other_revision, self.channel_id),
        )
        await self.db.conn.commit()
        with self.assertRaises(DraftConflictError):
            await self.db.publish_channel_custom_draft(
                channel_id=self.channel_id, published_by=101
            )


class DraftStaticTests(unittest.TestCase):
    def test_schema_v26_and_tables_are_registered(self):
        source = Path("database.py").read_text(encoding="utf-8")
        self.assertIn('Migration(26, "channel_custom_drafts", apply_custom_drafts_v26)', source)
        self.assertIn("CREATE TABLE channel_custom_drafts", source)
        self.assertIn("CREATE TABLE channel_custom_draft_items", source)
        self.assertIn("draft_published", source)

    def test_handlers_stage_edits_instead_of_live_override(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn("set_channel_custom_draft_template_text", source)
        self.assertIn("set_channel_custom_draft_start_card_media", source)
        self.assertIn("stage_channel_custom_start_card_media_removal", source)
        self.assertNotIn("await db.set_template_override(channel_id=cid,template_key=key", source)

    def test_publish_and_discard_are_explicit_owner_actions(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('callback_data=f"custom:publish:{channel_id}"', source)
        self.assertIn('callback_data=f"custom:discard:{channel_id}"', source)
        self.assertIn("publish_channel_custom_draft", source)
        self.assertIn("discard_channel_custom_draft", source)


if __name__ == "__main__":
    unittest.main()
