import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS, Database
from templates import TEMPLATE_REGISTRY


class CustomPackFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "bot.sqlite3")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _build_v22_fixture(self) -> tuple[int, int]:
        db = Database(self.db_path, migrations=DEFAULT_MIGRATIONS[:22])
        await db.init()
        _, a = await db.register_channel(
            owner_id=101,
            group_id=-100101,
            group_title="A",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="AnonA",
        )
        _, b = await db.register_channel(
            owner_id=202,
            group_id=-100202,
            group_title="B",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="AnonB",
        )
        a_id, b_id = int(a["channel_id"]), int(b["channel_id"])
        await db.set_template_override(
            channel_id=a_id,
            template_key="start.greeting",
            custom_text="[A] {channel_name}",
            updated_by=101,
        )
        # Preserve an unknown legacy key rather than silently dropping it.
        async with db._write_lock:
            await db.conn.execute(
                "INSERT INTO channel_template_overrides(channel_id,template_key,custom_text,updated_at,updated_by) VALUES(?,?,?,?,?)",
                (a_id, "legacy.unknown", "legacy text", "2026-01-01T00:00:00+00:00", 101),
            )
            await db.conn.commit()
        await db.close()
        return a_id, b_id

    async def test_v23_seeds_standard_and_existing_channel_snapshots(self) -> None:
        a_id, b_id = await self._build_v22_fixture()
        db = Database(self.db_path)
        await db.init()
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 23)

        standard_state = await db.get_standard_custom_state()
        self.assertIsNotNone(standard_state)
        standard_items = await db.get_standard_custom_items()
        self.assertEqual(
            standard_items["template:start.greeting"]["payload"]["text"],
            TEMPLATE_REGISTRY["start.greeting"].default,
        )

        a_state = await db.get_channel_custom_state(a_id)
        b_state = await db.get_channel_custom_state(b_id)
        self.assertEqual(int(a_state["active_revision_id"]), int(a_state["initial_revision_id"]))
        self.assertEqual(int(b_state["active_revision_id"]), int(b_state["initial_revision_id"]))
        # Later schema migrations may advance the active Standard revision
        # without mutating the immutable migration snapshot already assigned
        # to an existing channel.
        self.assertLessEqual(
            int(a_state["source_standard_revision_id"]),
            int(standard_state["active_revision_id"]),
        )
        self.assertIsNotNone(await db.get_standard_custom_revision(
            int(a_state["source_standard_revision_id"])
        ))

        a_items = await db.get_channel_custom_items(
            channel_id=a_id, include_legacy_template_overlay=False
        )
        b_items = await db.get_channel_custom_items(
            channel_id=b_id, include_legacy_template_overlay=False
        )
        self.assertEqual(a_items["template:start.greeting"]["payload"]["text"], "[A] {channel_name}")
        self.assertEqual(
            b_items["template:start.greeting"]["payload"]["text"],
            TEMPLATE_REGISTRY["start.greeting"].default,
        )
        self.assertEqual(
            a_items["template:legacy.unknown"]["item_type"],
            "legacy_template_override",
        )
        self.assertEqual(
            a_items["template:legacy.unknown"]["payload"]["text"],
            "legacy text",
        )
        await db.run_preflight()
        await db.close()

    async def test_legacy_overlay_keeps_foundation_view_current_until_full_cutover(self) -> None:
        a_id, _ = await self._build_v22_fixture()
        db = Database(self.db_path)
        await db.init()
        snapshot = await db.get_channel_custom_template_text(
            channel_id=a_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        self.assertEqual(snapshot, "[A] {channel_name}")

        await db.set_template_override(
            channel_id=a_id,
            template_key="start.greeting",
            custom_text="[A2] {channel_name}",
            updated_by=101,
        )
        overlay = await db.get_channel_custom_template_text(
            channel_id=a_id,
            template_key="start.greeting",
            include_legacy_template_overlay=True,
        )
        immutable_snapshot = await db.get_channel_custom_template_text(
            channel_id=a_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        self.assertEqual(overlay, "[A2] {channel_name}")
        self.assertEqual(immutable_snapshot, "[A] {channel_name}")
        await db.close()

    async def test_db_foreign_key_blocks_cross_channel_active_revision(self) -> None:
        a_id, b_id = await self._build_v22_fixture()
        db = Database(self.db_path)
        await db.init()
        b_state = await db.get_channel_custom_state(b_id)
        with self.assertRaises(sqlite3.IntegrityError):
            await db.conn.execute(
                "UPDATE channel_custom_state SET active_revision_id=? WHERE channel_id=?",
                (int(b_state["active_revision_id"]), a_id),
            )
        await db.conn.rollback()
        await db.run_preflight()
        await db.close()

    async def test_migration_audit_is_seeded_without_forging_actor(self) -> None:
        a_id, _ = await self._build_v22_fixture()
        db = Database(self.db_path)
        await db.init()
        rows = await db.list_customization_audit(channel_id=a_id)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope_type"], "channel_custom")
        self.assertEqual(rows[0]["action"], "migration_seed")
        self.assertIsNone(rows[0]["actor_user_id"])
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertGreater(metadata["items"], 0)
        await db.close()


if __name__ == "__main__":
    unittest.main()
