import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from handlers import TopicCleaner


def _row():
    return {
        "channel_id": 5,
        "user_id": 42,
        "privacy_mode": "identified",
        "group_id": -1001,
        "topic_id": 77,
    }


def _bad_request(message: str):
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


class TopicCleanerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_preserves_mapping_and_marks_topic_closed(self):
        bot = SimpleNamespace(close_forum_topic=AsyncMock())
        db = SimpleNamespace(
            mark_topic_auto_closed=AsyncMock(),
            delete_topic_mapping=AsyncMock(),
        )
        cleaner = TopicCleaner(bot=bot, db=db)

        result = await cleaner._remove_topic(_row(), action="close")

        self.assertEqual(result, "closed")
        db.mark_topic_auto_closed.assert_awaited_once_with(
            channel_id=5, user_id=42, privacy_mode="identified"
        )
        db.delete_topic_mapping.assert_not_awaited()

    async def test_already_closed_topic_is_not_misclassified_as_stale(self):
        bot = SimpleNamespace(
            close_forum_topic=AsyncMock(
                side_effect=_bad_request("Bad Request: topic closed")
            )
        )
        db = SimpleNamespace(
            mark_topic_auto_closed=AsyncMock(),
            delete_topic_mapping=AsyncMock(),
        )
        cleaner = TopicCleaner(bot=bot, db=db)

        result = await cleaner._remove_topic(_row(), action="close")

        self.assertEqual(result, "closed")
        db.mark_topic_auto_closed.assert_awaited_once()
        db.delete_topic_mapping.assert_not_awaited()

    async def test_delete_reopens_closed_topic_before_retrying_delete(self):
        bot = SimpleNamespace(
            delete_forum_topic=AsyncMock(
                side_effect=[_bad_request("Bad Request: topic closed"), None]
            ),
            reopen_forum_topic=AsyncMock(),
        )
        db = SimpleNamespace(delete_topic_mapping=AsyncMock())
        cleaner = TopicCleaner(bot=bot, db=db)

        result = await cleaner._remove_topic(_row(), action="delete")

        self.assertEqual(result, "deleted")
        bot.reopen_forum_topic.assert_awaited_once_with(
            chat_id=-1001, message_thread_id=77
        )
        self.assertEqual(bot.delete_forum_topic.await_count, 2)
        db.delete_topic_mapping.assert_awaited_once_with(
            channel_id=5, user_id=42, privacy_mode="identified"
        )

    async def test_missing_topic_removes_only_stale_mapping(self):
        bot = SimpleNamespace(
            delete_forum_topic=AsyncMock(
                side_effect=_bad_request("Bad Request: message thread not found")
            ),
            reopen_forum_topic=AsyncMock(),
        )
        db = SimpleNamespace(delete_topic_mapping=AsyncMock())
        cleaner = TopicCleaner(bot=bot, db=db)

        result = await cleaner._remove_topic(_row(), action="delete")

        self.assertEqual(result, "stale")
        bot.reopen_forum_topic.assert_not_awaited()
        db.delete_topic_mapping.assert_awaited_once_with(
            channel_id=5, user_id=42, privacy_mode="identified"
        )


if __name__ == "__main__":
    unittest.main()
