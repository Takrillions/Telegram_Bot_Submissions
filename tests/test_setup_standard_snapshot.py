import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database
from templates import TEMPLATE_REGISTRY


class SetupStandardSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "bot.sqlite3")
        self.db = Database(self.db_path)
        await self.db.init()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def _register(self, *, owner_id: int, group_id: int, title: str):
        return await self.db.register_channel(
            owner_id=owner_id,
            group_id=group_id,
            group_title=title,
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="Asia/Tashkent",
            anonymous_prefix="Anon",
        )

    async def _activate_standard_v2(self, greeting: str) -> int:
        async with self.db._write_lock:
            state = await self.db.get_standard_custom_state()
            previous = int(state["active_revision_id"])
            cursor = await self.db.conn.execute(
                """INSERT INTO bot_standard_custom_revisions(
                       created_at,created_by,source,summary
                   ) VALUES(?,?,?,?)""",
                ("2026-08-16T00:00:00+00:00", 900000001, "test_standard_update", "v2"),
            )
            revision_id = int(cursor.lastrowid)
            await self.db.conn.execute(
                """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
                   SELECT ?,item_key,item_type,payload_json
                   FROM bot_standard_custom_items WHERE revision_id=?""",
                (revision_id, previous),
            )
            spec = TEMPLATE_REGISTRY["start.greeting"]
            await self.db.conn.execute(
                """UPDATE bot_standard_custom_items
                   SET payload_json=?
                   WHERE revision_id=? AND item_key='template:start.greeting'""",
                (
                    json.dumps(
                        {"scope": spec.scope, "text": greeting},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    revision_id,
                ),
            )
            await self.db.conn.execute(
                """UPDATE bot_standard_custom_state
                   SET active_revision_id=?,updated_at=?,updated_by=? WHERE singleton_id=1""",
                (revision_id, "2026-08-16T00:00:00+00:00", 900000001),
            )
            await self.db.conn.commit()
        return revision_id

    async def test_new_channel_gets_immutable_snapshot_of_current_standard(self) -> None:
        standard_v1 = int((await self.db.get_standard_custom_state())["active_revision_id"])
        status_a, channel_a = await self._register(owner_id=101, group_id=-100101, title="A")
        self.assertEqual(status_a, "created")
        a_id = int(channel_a["channel_id"])
        a_state = await self.db.get_channel_custom_state(a_id)
        self.assertEqual(int(a_state["source_standard_revision_id"]), standard_v1)
        self.assertEqual(int(a_state["active_revision_id"]), int(a_state["initial_revision_id"]))

        greeting_v2 = "[V2] Добро пожаловать в {channel_name}."
        standard_v2 = await self._activate_standard_v2(greeting_v2)
        status_b, channel_b = await self._register(owner_id=202, group_id=-100202, title="B")
        self.assertEqual(status_b, "created")
        b_id = int(channel_b["channel_id"])
        b_state = await self.db.get_channel_custom_state(b_id)
        self.assertEqual(int(b_state["source_standard_revision_id"]), standard_v2)

        a_text = await self.db.get_channel_custom_template_text(
            channel_id=a_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        b_text = await self.db.get_channel_custom_template_text(
            channel_id=b_id,
            template_key="start.greeting",
            include_legacy_template_overlay=False,
        )
        self.assertEqual(a_text, TEMPLATE_REGISTRY["start.greeting"].default)
        self.assertEqual(b_text, greeting_v2)
        await self.db.run_preflight()

    async def test_setup_snapshot_records_owner_and_audit(self) -> None:
        status, channel = await self._register(owner_id=303, group_id=-100303, title="Audit")
        self.assertEqual(status, "created")
        channel_id = int(channel["channel_id"])
        state = await self.db.get_channel_custom_state(channel_id)
        revision = await (await self.db.conn.execute(
            "SELECT * FROM channel_custom_revisions WHERE revision_id=? AND channel_id=?",
            (int(state["initial_revision_id"]), channel_id),
        )).fetchone()
        self.assertEqual(revision["source"], "setup_snapshot")
        self.assertEqual(int(revision["created_by"]), 303)

        audit = await self.db.list_customization_audit(channel_id=channel_id)
        self.assertEqual(audit[0]["action"], "setup_snapshot")
        self.assertEqual(int(audit[0]["actor_user_id"]), 303)
        metadata = json.loads(audit[0]["metadata_json"])
        self.assertEqual(metadata["revision_id"], int(state["initial_revision_id"]))
        self.assertEqual(metadata["source_standard_revision_id"], int(state["source_standard_revision_id"]))
        self.assertGreater(metadata["items"], 0)

    async def test_repeated_setup_does_not_replace_channel_custom_snapshot(self) -> None:
        status, channel = await self._register(owner_id=404, group_id=-100404, title="Before")
        self.assertEqual(status, "created")
        channel_id = int(channel["channel_id"])
        original_state = await self.db.get_channel_custom_state(channel_id)
        original_revision = int(original_state["initial_revision_id"])
        before_count = int((await (await self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM channel_custom_revisions WHERE channel_id=?", (channel_id,)
        )).fetchone())["c"])

        status, updated = await self._register(owner_id=404, group_id=-100404, title="After")
        self.assertEqual(status, "existing")
        self.assertEqual(updated["group_title"], "After")
        after_state = await self.db.get_channel_custom_state(channel_id)
        after_count = int((await (await self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM channel_custom_revisions WHERE channel_id=?", (channel_id,)
        )).fetchone())["c"])
        self.assertEqual(int(after_state["initial_revision_id"]), original_revision)
        self.assertEqual(int(after_state["active_revision_id"]), int(original_state["active_revision_id"]))
        self.assertEqual(after_count, before_count)

    async def test_snapshot_failure_rolls_back_entire_new_channel(self) -> None:
        await self.db.conn.execute(
            """CREATE TRIGGER force_setup_snapshot_failure
               BEFORE INSERT ON channel_custom_revisions
               WHEN NEW.source='setup_snapshot'
               BEGIN
                   SELECT RAISE(ABORT, 'forced setup snapshot failure');
               END"""
        )
        await self.db.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            await self._register(owner_id=505, group_id=-100505, title="Must Roll Back")

        raw_channel = await (await self.db.conn.execute(
            "SELECT * FROM channels WHERE group_id=-100505"
        )).fetchone()
        self.assertIsNone(raw_channel)
        self.assertEqual(
            int((await (await self.db.conn.execute(
                "SELECT COUNT(*) AS c FROM channel_anonymous_counters"
            )).fetchone())["c"]),
            0,
        )
        self.assertEqual(
            int((await (await self.db.conn.execute(
                "SELECT COUNT(*) AS c FROM channel_custom_state"
            )).fetchone())["c"]),
            0,
        )
        await self.db.run_preflight()


if __name__ == "__main__":
    unittest.main()
