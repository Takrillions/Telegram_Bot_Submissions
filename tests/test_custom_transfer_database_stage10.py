from pathlib import Path
import json
import tempfile
import unittest

from custom_transfer import (
    CUSTOM_PACK_SCHEMA_VERSION,
    build_export_document,
    dumps_export_document,
    normalize_import_document,
)
from database import CURRENT_SCHEMA_VERSION, Database, DraftNotEmptyError
from templates import TEMPLATE_REGISTRY


class CustomTransferDatabaseStage10Tests(unittest.IsolatedAsyncioTestCase):
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

    def _pack(self, *, greeting="IMPORTED {channel_name}", media=True, source_channel_id=777):
        texts = {
            key: spec.default
            for key, spec in TEMPLATE_REGISTRY.items()
            if spec.scope == "channel"
        }
        texts["start.greeting"] = greeting
        document = build_export_document(
            channel_id=source_channel_id,
            channel_title="External source",
            revision_id=55,
            source_standard_revision_id=7,
            template_texts=texts,
            media=(
                {"media_type": "animation", "media_file_id": "imported-gif-id"}
                if media else None
            ),
        )
        return normalize_import_document(document)

    async def test_schema_includes_v29_custom_transfer(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 29)
        history = await (await self.db.conn.execute(
            "SELECT version,name FROM schema_migrations WHERE version=29"
        )).fetchone()
        self.assertIsNotNone(history)
        self.assertEqual(int(history["version"]), 29)
        self.assertEqual(str(history["name"]), "custom_transfer_json")

    async def test_export_reads_only_published_revision_and_audits(self):
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            custom_text="PUBLISHED {channel_name}",
            updated_by=101,
        )
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=self.target_id,
            media_type="photo",
            media_file_id="published-photo",
            updated_by=101,
        )
        published_revision = await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        # New unsaved draft must never leak into export.
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            custom_text="UNPUBLISHED {channel_name}",
            updated_by=101,
        )

        document = await self.db.export_channel_custom_pack(
            channel_id=self.target_id, exported_by=101
        )
        self.assertEqual(int(document["source_revision_id"]), published_revision)
        self.assertEqual(document["start_card"]["text"], "PUBLISHED {channel_name}")
        self.assertEqual(document["start_card"]["media"]["telegram_file_id"], "published-photo")
        raw = dumps_export_document(document).decode("utf-8")
        self.assertNotIn("UNPUBLISHED", raw)
        self.assertNotIn('"owner_id"', raw)
        self.assertNotIn('"group_id"', raw)
        self.assertNotIn('"bot_token"', raw.lower())

        event = await (await self.db.conn.execute(
            "SELECT action,metadata_json FROM customization_audit_log WHERE channel_id=? AND action='custom_exported' ORDER BY event_id DESC LIMIT 1",
            (self.target_id,),
        )).fetchone()
        self.assertIsNotNone(event)
        metadata = json.loads(str(event["metadata_json"]))
        self.assertEqual(int(metadata["revision_id"]), published_revision)
        self.assertEqual(int(metadata["schema_version"]), CUSTOM_PACK_SCHEMA_VERSION)
        self.assertEqual(len(str(metadata["document_sha256"])), 64)

    async def test_export_foreign_actor_is_denied(self):
        with self.assertRaises(PermissionError):
            await self.db.export_channel_custom_pack(
                channel_id=self.target_id, exported_by=999
            )
        count = await (await self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM customization_audit_log WHERE channel_id=? AND action='custom_exported'",
            (self.target_id,),
        )).fetchone()
        self.assertEqual(int(count["n"]), 0)

    async def test_import_is_draft_only_then_publishes_as_import_revision(self):
        live_before = await self.db.get_channel_custom_template_text(
            channel_id=self.target_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        state_before = await self.db.get_channel_custom_state(self.target_id)
        old_standard_lineage = state_before["source_standard_revision_id"]
        pack = self._pack()

        plan = await self.db.plan_channel_custom_import(
            channel_id=self.target_id, actor_id=101, pack=pack
        )
        self.assertIn("template:start.greeting", plan["changed_keys"])
        self.assertIn("start_card.media", plan["changed_keys"])
        self.assertGreater(int(plan["staged"]), 0)

        result = await self.db.stage_channel_custom_import(
            channel_id=self.target_id, imported_by=101, pack=pack
        )
        self.assertGreater(int(result["staged"]), 0)
        # Live stays untouched until explicit publish.
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            live_before,
        )
        self.assertIsNone(await self.db.get_channel_custom_start_card_media(self.target_id))
        draft = await self.db.get_channel_custom_draft_state(self.target_id)
        self.assertEqual(str(draft["publish_source"]), "import")
        self.assertIsNone(draft["source_channel_id"])

        event = await (await self.db.conn.execute(
            "SELECT metadata_json FROM customization_audit_log WHERE channel_id=? AND action='custom_imported' ORDER BY event_id DESC LIMIT 1",
            (self.target_id,),
        )).fetchone()
        metadata = json.loads(str(event["metadata_json"]))
        self.assertEqual(metadata["status"], "staged")
        self.assertEqual(int(metadata["source_channel_id"]), 777)
        self.assertEqual(int(metadata["source_revision_id"]), 55)

        revision_id = await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        revision = await self.db.get_channel_custom_revision(
            channel_id=self.target_id, revision_id=revision_id
        )
        self.assertEqual(str(revision["source"]), "import")
        self.assertEqual(
            revision["source_standard_revision_id"], old_standard_lineage
        )
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=self.target_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            "IMPORTED {channel_name}",
        )
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(self.target_id),
            {"media_type": "animation", "media_file_id": "imported-gif-id"},
        )

    async def test_import_source_metadata_cannot_change_target_owner_or_other_channel(self):
        status, other = await self.db.register_channel(
            owner_id=202,
            group_id=-10002,
            group_title="Other",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Other",
        )
        self.assertEqual(status, "created")
        other_id = int(other["channel_id"])
        other_before = await self.db.get_channel_custom_template_text(
            channel_id=other_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        # The source id deliberately points at someone else's real channel.
        pack = self._pack(source_channel_id=other_id)
        await self.db.stage_channel_custom_import(
            channel_id=self.target_id, imported_by=101, pack=pack
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        target_row = await self.db.get_channel_by_id(self.target_id)
        other_row = await self.db.get_channel_by_id(other_id)
        self.assertEqual(int(target_row["owner_id"]), 101)
        self.assertEqual(int(other_row["owner_id"]), 202)
        self.assertEqual(
            await self.db.get_channel_custom_template_text(
                channel_id=other_id,
                template_key="start.greeting",
                include_legacy_template_overlay=False,
            ),
            other_before,
        )

    async def test_foreign_actor_cannot_plan_or_stage_import(self):
        pack = self._pack()
        with self.assertRaises(PermissionError):
            await self.db.plan_channel_custom_import(
                channel_id=self.target_id, actor_id=999, pack=pack
            )
        with self.assertRaises(PermissionError):
            await self.db.stage_channel_custom_import(
                channel_id=self.target_id, imported_by=999, pack=pack
            )
        self.assertFalse(await self.db.has_channel_custom_draft(self.target_id))

    async def test_existing_draft_is_never_overwritten_by_import(self):
        await self.db.set_channel_custom_draft_template_text(
            channel_id=self.target_id,
            template_key="message.received",
            custom_text="MY UNSAVED WORK",
            updated_by=101,
        )
        pack = self._pack()
        with self.assertRaises(DraftNotEmptyError):
            await self.db.stage_channel_custom_import(
                channel_id=self.target_id, imported_by=101, pack=pack
            )
        self.assertEqual(
            await self.db.get_channel_custom_draft_template_text(
                channel_id=self.target_id, template_key="message.received"
            ),
            "MY UNSAVED WORK",
        )

    async def test_import_without_media_stages_removal_not_immediate_delete(self):
        await self.db.set_channel_custom_draft_start_card_media(
            channel_id=self.target_id,
            media_type="video",
            media_file_id="live-video",
            updated_by=101,
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        pack = self._pack(media=False)
        plan = await self.db.plan_channel_custom_import(
            channel_id=self.target_id, actor_id=101, pack=pack
        )
        self.assertIn("start_card.media", plan["changed_keys"])
        await self.db.stage_channel_custom_import(
            channel_id=self.target_id, imported_by=101, pack=pack
        )
        self.assertEqual(
            await self.db.get_channel_custom_start_card_media(self.target_id),
            {"media_type": "video", "media_file_id": "live-video"},
        )
        await self.db.publish_channel_custom_draft(
            channel_id=self.target_id, published_by=101
        )
        self.assertIsNone(await self.db.get_channel_custom_start_card_media(self.target_id))
