import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from broadcast_runtime import BroadcastRuntime
from database import Database
from handlers import is_general_forum_message
from aiogram.enums import ChatType


class BroadcastRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        await self.db.init()
        _, channel = await self.db.register_channel(
            owner_id=10,
            group_id=-1001,
            group_title="A",
            default_reset_days=30,
            default_notice_text="notice",
            default_timezone="UTC",
        )
        self.channel_id = int(channel["channel_id"])
        for user_id in (101, 102, 103):
            await self.db.upsert_user(
                user_id=user_id, first_name=f"U{user_id}", last_name=None, username=None
            )
            await self.db.attach_subscriber(channel_id=self.channel_id, user_id=user_id)

        # 101 has both privacy topics, but anonymous is current.
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=101, privacy_mode="identified",
            group_id=-1001, topic_id=11,
        )
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=101, privacy_mode="anonymous",
            group_id=-1001, topic_id=12,
        )
        await self.db.set_privacy_mode(
            channel_id=self.channel_id, user_id=101, privacy_mode="anonymous"
        )

        # 102 has a current identified topic; tests may close or fail it.
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=102, privacy_mode="identified",
            group_id=-1001, topic_id=22,
        )
        await self.db.set_privacy_mode(
            channel_id=self.channel_id, user_id=102, privacy_mode="identified"
        )

        # 103 once had an anonymous topic but is currently identified.  There is
        # deliberately no identified topic: broadcast must never fall back to 33.
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=103, privacy_mode="anonymous",
            group_id=-1001, topic_id=33,
        )
        await self.db.set_privacy_mode(
            channel_id=self.channel_id, user_id=103, privacy_mode="identified"
        )

    async def asyncTearDown(self):
        try:
            await self.db.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)

    async def _draft_and_claim(self):
        draft = await self.db.create_broadcast_draft(
            channel_id=self.channel_id,
            created_by=10,
            source_chat_id=-1001,
            source_message_id=500,
        )
        broadcast_id = str(draft["broadcast_id"])
        self.assertTrue(await self.db.claim_broadcast_for_send(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10
        ))
        return broadcast_id

    async def test_current_privacy_is_used_once_and_stale_other_topic_is_not_fallback(self):
        await self.db.set_topic_status(
            channel_id=self.channel_id, user_id=102, privacy_mode="identified", status="closed"
        )
        broadcast_id = await self._draft_and_claim()
        bot = SimpleNamespace(copy_message=AsyncMock())
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )

        self.assertEqual(summary.unique_recipients, 3)
        self.assertEqual(summary.delivered, 1)
        self.assertEqual(summary.undelivered, 2)
        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.skipped, 0)
        bot.copy_message.assert_awaited_once_with(
            chat_id=-1001, message_thread_id=12, from_chat_id=-1001, message_id=500
        )
        sent_threads = [call.kwargs["message_thread_id"] for call in bot.copy_message.await_args_list]
        self.assertNotIn(11, sent_threads)
        self.assertNotIn(33, sent_threads)

    async def test_one_telegram_error_does_not_stop_other_recipients(self):
        broadcast_id = await self._draft_and_claim()

        async def copy_message(**kwargs):
            if kwargs["message_thread_id"] == 12:
                raise RuntimeError("network failure")
            return None

        bot = SimpleNamespace(copy_message=AsyncMock(side_effect=copy_message))
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )
        self.assertEqual(summary.unique_recipients, 3)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.delivered, 1)
        self.assertEqual(summary.undelivered, 1)
        self.assertEqual(bot.copy_message.await_count, 2)

    async def test_duplicate_claim_and_conflicting_broadcast_are_rejected(self):
        first = await self._draft_and_claim()
        self.assertFalse(await self.db.claim_broadcast_for_send(
            broadcast_id=first, channel_id=self.channel_id, created_by=10
        ))
        second = await self.db.create_broadcast_draft(
            channel_id=self.channel_id, created_by=10, source_chat_id=-1001, source_message_id=501
        )
        self.assertFalse(await self.db.claim_broadcast_for_send(
            broadcast_id=str(second["broadcast_id"]), channel_id=self.channel_id, created_by=10
        ))

    async def test_resume_never_resends_already_reserved_recipient(self):
        broadcast_id = await self._draft_and_claim()
        target = await self.db.reserve_broadcast_delivery(
            broadcast_id=broadcast_id, channel_id=self.channel_id, user_id=101
        )
        self.assertEqual(target["topic_id"], 12)

        bot = SimpleNamespace(copy_message=AsyncMock())
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )
        sent_threads = [call.kwargs["message_thread_id"] for call in bot.copy_message.await_args_list]
        self.assertNotIn(12, sent_threads)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.unique_recipients, 3)


    async def test_album_source_is_persisted_and_delivered_as_one_group(self):
        draft = await self.db.create_broadcast_draft(
            channel_id=self.channel_id, created_by=10, source_chat_id=-1001, source_message_id=500,
            source_message_ids=[500, 501, 502], source_media_group_id="album-1",
        )
        broadcast_id = str(draft["broadcast_id"])
        self.assertEqual(self.db.broadcast_source_message_ids(draft), (500, 501, 502))
        self.assertEqual(str(draft["source_media_group_id"]), "album-1")
        self.assertTrue(await self.db.claim_broadcast_for_send(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10
        ))

        bot = SimpleNamespace(
            copy_message=AsyncMock(),
            copy_messages=AsyncMock(return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2), SimpleNamespace(message_id=3)]),
        )
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )

        self.assertEqual(summary.delivered, 2)
        self.assertEqual(summary.undelivered, 1)
        bot.copy_message.assert_not_awaited()
        self.assertEqual(bot.copy_messages.await_count, 2)
        for call in bot.copy_messages.await_args_list:
            self.assertEqual(call.kwargs["message_ids"], [500, 501, 502])
        sent_threads = {call.kwargs["message_thread_id"] for call in bot.copy_messages.await_args_list}
        self.assertEqual(sent_threads, {12, 22})

    async def test_album_partial_copy_is_recorded_as_error_without_retry(self):
        draft = await self.db.create_broadcast_draft(
            channel_id=self.channel_id, created_by=10, source_chat_id=-1001, source_message_id=500,
            source_message_ids=[500, 501], source_media_group_id="album-2",
        )
        broadcast_id = str(draft["broadcast_id"])
        self.assertTrue(await self.db.claim_broadcast_for_send(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10
        ))
        bot = SimpleNamespace(
            copy_message=AsyncMock(),
            copy_messages=AsyncMock(return_value=[SimpleNamespace(message_id=1)]),
        )
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )
        self.assertEqual(summary.errors, 2)
        self.assertEqual(summary.undelivered, 1)
        self.assertEqual(summary.delivered, 0)

    async def test_legacy_single_source_still_uses_copy_message(self):
        broadcast_id = await self._draft_and_claim()
        bot = SimpleNamespace(copy_message=AsyncMock(), copy_messages=AsyncMock())
        summary = await BroadcastRuntime(bot=bot, db=self.db).deliver(
            broadcast_id=broadcast_id, channel_id=self.channel_id
        )
        self.assertEqual(summary.delivered, 2)
        bot.copy_message.assert_awaited()
        bot.copy_messages.assert_not_awaited()

    async def test_draft_edit_and_cancel_are_owner_and_state_scoped(self):
        draft = await self.db.create_broadcast_draft(
            channel_id=self.channel_id, created_by=10, source_chat_id=-1001, source_message_id=500
        )
        broadcast_id = str(draft["broadcast_id"])
        self.assertFalse(await self.db.update_broadcast_draft_source(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=99,
            source_chat_id=-1001, source_message_id=501,
        ))
        self.assertTrue(await self.db.update_broadcast_draft_source(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10,
            source_chat_id=-1001, source_message_id=501,
        ))
        self.assertTrue(await self.db.cancel_broadcast_draft(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10
        ))
        self.assertFalse(await self.db.claim_broadcast_for_send(
            broadcast_id=broadcast_id, channel_id=self.channel_id, created_by=10
        ))


class BroadcastContextTests(unittest.TestCase):
    def test_only_general_context_is_accepted(self):
        general_without_thread = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.SUPERGROUP), message_thread_id=None, is_topic_message=False
        )
        general_reply_thread = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.SUPERGROUP), message_thread_id=731, is_topic_message=False
        )
        user_topic = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.SUPERGROUP), message_thread_id=77, is_topic_message=True
        )
        unknown_forum_topic = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.SUPERGROUP), message_thread_id=999, is_topic_message=True
        )
        private = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE), message_thread_id=None, is_topic_message=False
        )
        self.assertTrue(is_general_forum_message(general_without_thread))
        self.assertTrue(is_general_forum_message(general_reply_thread))
        self.assertFalse(is_general_forum_message(user_topic))
        self.assertFalse(is_general_forum_message(unknown_forum_topic))
        self.assertFalse(is_general_forum_message(private))


if __name__ == "__main__":
    unittest.main()
