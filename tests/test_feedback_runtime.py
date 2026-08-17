import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers import BufferedMessage, FeedbackRuntime


def _message(
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    media_group_id: str | None = None,
    photo: bool = False,
):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            first_name="User",
            last_name=None,
            username=None,
        ),
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        media_group_id=media_group_id,
        date=datetime.now(timezone.utc),
        photo=[object()] if photo else None,
        video=None,
        voice=None,
        document=None,
        audio=None,
        sticker=None,
        animation=None,
        text=None if photo else "hello",
    )


def _db():
    return SimpleNamespace(
        upsert_user=AsyncMock(),
        touch_subscriber=AsyncMock(),
        get_anonymous_tag=AsyncMock(return_value="Anon-7"),
        ensure_anonymous_tag=AsyncMock(return_value="Anon-7"),
        get_template_override=AsyncMock(return_value=None),
        record_message_event=AsyncMock(),
        record_reaction_source=AsyncMock(),
        touch_topic=AsyncMock(),
        set_user_blocked=AsyncMock(),
        set_topic_status=AsyncMock(),
        mark_topic_answered=AsyncMock(),
        active_subscriber_restriction_details=AsyncMock(return_value=None),
        get_subscriber_moderation=AsyncMock(return_value=None),
    )


def _bot():
    return SimpleNamespace(
        copy_message=AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=int(kwargs["message_id"]) + 1000)),
        copy_messages=AsyncMock(side_effect=lambda **kwargs: [SimpleNamespace(message_id=int(mid) + 1000) for mid in kwargs["message_ids"]]),
        send_message=AsyncMock(),
    )


class FeedbackRuntimeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_message_reaches_topic_and_records_real_conversation(self):
        db = _db()
        bot = _bot()
        runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=0.05)
        runtime.get_or_create_topic = AsyncMock(return_value=(-1001, 77))

        message = _message(user_id=42, chat_id=42, message_id=10)
        await runtime.accept_user_message(
            message=message,
            channel_id=5,
            group_id=-1001,
            privacy_mode="anonymous",
        )

        bot.copy_message.assert_awaited_once_with(
            chat_id=-1001,
            message_thread_id=77,
            from_chat_id=42,
            message_id=10,
        )
        event = db.record_message_event.await_args.kwargs
        self.assertEqual(event["conversation_id"], 77)
        self.assertEqual(event["privacy_mode"], "anonymous")
        self.assertEqual(event["direction"], "subscriber_to_admin")
        db.record_reaction_source.assert_awaited_once_with(
            channel_id=5, group_id=-1001, forum_message_id=1010, user_id=42,
            privacy_mode="anonymous", private_chat_id=42, private_message_id=10, topic_id=77,
        )
        bot.send_message.assert_awaited_once()
        acknowledgement = bot.send_message.await_args.kwargs
        self.assertEqual(acknowledgement["chat_id"], 42)
        self.assertIn("получила ваше сообщение", acknowledgement["text"])

    async def test_admin_reply_reaches_subscriber_with_same_conversation_and_privacy(self):
        db = _db()
        bot = _bot()
        runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=0.05)

        message = _message(user_id=700, chat_id=-1001, message_id=20)
        await runtime.accept_admin_message(
            message=message,
            channel_id=5,
            user_id=42,
            group_id=-1001,
            topic_id=77,
            privacy_mode="anonymous",
        )

        bot.copy_message.assert_awaited_once_with(
            chat_id=42,
            from_chat_id=-1001,
            message_id=20,
        )
        event = db.record_message_event.await_args.kwargs
        self.assertEqual(event["conversation_id"], 77)
        self.assertEqual(event["privacy_mode"], "anonymous")
        self.assertEqual(event["direction"], "admin_to_subscriber")
        db.mark_topic_answered.assert_awaited_once_with(
            channel_id=5,
            user_id=42,
            privacy_mode="anonymous",
        )

    async def test_subscriber_album_is_copied_once_and_each_item_uses_topic_conversation(self):
        db = _db()
        bot = _bot()
        runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=0.05)
        runtime.get_or_create_topic = AsyncMock(return_value=(-1001, 88))

        first = _message(
            user_id=42,
            chat_id=42,
            message_id=30,
            media_group_id="album-1",
            photo=True,
        )
        second = _message(
            user_id=42,
            chat_id=42,
            message_id=31,
            media_group_id="album-1",
            photo=True,
        )

        await runtime.accept_user_message(
            message=first,
            channel_id=5,
            group_id=-1001,
            privacy_mode="identified",
        )
        await runtime.accept_user_message(
            message=second,
            channel_id=5,
            group_id=-1001,
            privacy_mode="identified",
        )
        await runtime.close()

        bot.copy_messages.assert_awaited_once_with(
            chat_id=-1001,
            message_thread_id=88,
            from_chat_id=42,
            message_ids=[30, 31],
        )
        self.assertEqual(db.record_message_event.await_count, 2)
        for call in db.record_message_event.await_args_list:
            self.assertEqual(call.kwargs["conversation_id"], 88)
            self.assertEqual(call.kwargs["privacy_mode"], "identified")
            self.assertEqual(call.kwargs["media_group_id"], "album-1")
        # A media group is one subscriber action, therefore only one acknowledgement.
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 42)

    async def test_runtime_rechecks_active_restriction_before_forum_delivery(self):
        db = _db()
        db.active_subscriber_restriction_details = AsyncMock(
            return_value=("muted", datetime.now(timezone.utc), "spam", False)
        )
        bot = _bot()
        runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=0.05)
        runtime.get_or_create_topic = AsyncMock(return_value=(-1001, 77))

        message = _message(user_id=42, chat_id=42, message_id=35)
        await runtime.accept_user_message(
            message=message, channel_id=5, group_id=-1001, privacy_mode="identified"
        )

        bot.copy_message.assert_not_awaited()
        runtime.get_or_create_topic.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        db.record_message_event.assert_not_awaited()

    async def test_anonymous_and_identified_batches_keep_separate_topic_conversations(self):
        db = _db()
        bot = _bot()
        runtime = FeedbackRuntime(bot=bot, db=db, media_group_delay=0.05)

        async def resolve_topic(*, channel_id, user, privacy_mode, anonymous_tag=None):
            self.assertEqual(channel_id, 5)
            self.assertEqual(user.id, 42)
            return (-1001, 71 if privacy_mode == "anonymous" else 72)

        runtime.get_or_create_topic = AsyncMock(side_effect=resolve_topic)

        anonymous = BufferedMessage(
            message=_message(user_id=42, chat_id=42, message_id=40),
            channel_id=5,
            group_id=-1001,
            user_id=42,
            privacy_mode="anonymous",
        )
        identified = BufferedMessage(
            message=_message(user_id=42, chat_id=42, message_id=41),
            channel_id=5,
            group_id=-1001,
            user_id=42,
            privacy_mode="identified",
        )

        await runtime._flush_user_messages([anonymous])
        await runtime._flush_user_messages([identified])

        events = [call.kwargs for call in db.record_message_event.await_args_list]
        self.assertEqual(
            [(event["privacy_mode"], event["conversation_id"]) for event in events],
            [("anonymous", 71), ("identified", 72)],
        )
        self.assertEqual(
            [call.kwargs["message_thread_id"] for call in bot.copy_message.await_args_list],
            [71, 72],
        )


if __name__ == "__main__":
    unittest.main()
