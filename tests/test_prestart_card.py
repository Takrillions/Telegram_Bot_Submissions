import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database import CURRENT_SCHEMA_VERSION, Database
from prestart_card import (
    DEFAULT_PRESTART_DESCRIPTION,
    MAX_DESCRIPTION_LENGTH,
    apply_description,
    validate_description,
    validate_media,
)


class PreStartCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        await self.db.init()

    async def asyncTearDown(self):
        try:
            await self.db.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)

    async def test_global_card_draft_is_singleton_not_channel_scoped(self):
        await self.db.set_bot_prestart_description(description="Global text", updated_by=10)
        await self.db.set_bot_prestart_media(media_type="photo", media_file_id="file-1", updated_by=20)
        row = await self.db.get_bot_prestart_card()
        self.assertEqual(row["singleton_id"], 1)
        self.assertEqual(row["description_override"], "Global text")
        self.assertEqual((row["media_type"], row["media_file_id"]), ("photo", "file-1"))
        self.assertEqual(row["updated_by"], 20)

    async def test_remove_media_keeps_description_and_reset_removes_override(self):
        await self.db.set_bot_prestart_description(description="Custom", updated_by=10)
        await self.db.set_bot_prestart_media(media_type="video", media_file_id="video-1", updated_by=10)
        await self.db.remove_bot_prestart_media(updated_by=10)
        row = await self.db.get_bot_prestart_card()
        self.assertEqual(row["description_override"], "Custom")
        self.assertIsNone(row["media_type"])
        self.assertIsNone(row["media_file_id"])
        await self.db.reset_bot_prestart_card()
        self.assertIsNone(await self.db.get_bot_prestart_card())

    async def test_validation_enforces_telegram_description_limit_and_media_types(self):
        self.assertEqual(validate_description("  test  "), "test")
        with self.assertRaises(ValueError):
            validate_description("")
        with self.assertRaises(ValueError):
            validate_description("x" * (MAX_DESCRIPTION_LENGTH + 1))
        self.assertEqual(validate_media("animation", "abc"), ("animation", "abc"))
        with self.assertRaises(ValueError):
            validate_media("document", "abc")

    async def test_apply_description_calls_bot_api_only_after_validation(self):
        bot = SimpleNamespace(set_my_description=AsyncMock(return_value=True))
        result = await apply_description(bot, "Updated")
        self.assertEqual(result, "Updated")
        bot.set_my_description.assert_awaited_once_with(description="Updated")
        with self.assertRaises(ValueError):
            await apply_description(bot, "")
        self.assertEqual(bot.set_my_description.await_count, 1)

    async def test_schema_contains_prestart_card_migration(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 19)
        columns = {
            row["name"]
            for row in await (await self.db.conn.execute("PRAGMA table_info(bot_prestart_card)")).fetchall()
        }
        self.assertEqual(
            columns,
            {"singleton_id", "description_override", "media_type", "media_file_id", "updated_at", "updated_by"},
        )

    def test_default_description_is_valid(self):
        self.assertEqual(validate_description(DEFAULT_PRESTART_DESCRIPTION), DEFAULT_PRESTART_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
