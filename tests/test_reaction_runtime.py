import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from authorization import ChannelAuthorizer
from database import Database
from reaction_runtime import ReactionRuntime


def emoji(value: str):
    return SimpleNamespace(type="emoji", emoji=value)


def update(*, group_id: int, message_id: int, actor_id: int, when: datetime, old=(), new=()):
    return SimpleNamespace(
        chat=SimpleNamespace(id=group_id),
        message_id=message_id,
        user=SimpleNamespace(id=actor_id, is_bot=False),
        actor_chat=None,
        date=when,
        old_reaction=list(old),
        new_reaction=list(new),
    )


class ReactionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        await self.db.init()
        _, channel = await self.db.register_channel(
            owner_id=10, group_id=-1001, group_title="A", default_reset_days=30,
            default_notice_text="notice", default_timezone="UTC",
        )
        self.channel_id = int(channel["channel_id"])
        await self.db.upsert_user(user_id=42, first_name="User", last_name=None, username="user42")
        await self.db.attach_subscriber(channel_id=self.channel_id, user_id=42)
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=42, privacy_mode="identified",
            group_id=-1001, topic_id=77,
        )
        await self.db.record_reaction_source(
            channel_id=self.channel_id, group_id=-1001, forum_message_id=500,
            user_id=42, privacy_mode="identified", private_chat_id=42,
            private_message_id=100, topic_id=77,
        )
        self.admins = {10, 11, 12}

        async def member_resolver(group_id, user_id):
            return group_id == -1001 and user_id in self.admins

        self.authorizer = ChannelAuthorizer(db=self.db, member_resolver=member_resolver)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
            forward_message=AsyncMock(return_value=SimpleNamespace(message_id=901)),
            copy_message=AsyncMock(return_value=SimpleNamespace(message_id=902)),
        )
        self.runtime = ReactionRuntime(bot=self.bot, db=self.db, authorizer=self.authorizer)
        self.now = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        try:
            await self.db.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)

    async def test_mode1_notifies_subscriber_once_and_removal_does_nothing(self):
        first = update(group_id=-1001, message_id=500, actor_id=11, when=self.now, new=[emoji("👍")])
        self.assertEqual(await self.runtime.handle(first), "subscriber_notified")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 42)
        self.assertIn("👍", self.bot.send_message.await_args.kwargs["text"])

        self.assertEqual(await self.runtime.handle(first), "duplicate")
        self.assertEqual(self.bot.send_message.await_count, 1)

        removed = update(
            group_id=-1001, message_id=500, actor_id=11,
            when=self.now + timedelta(seconds=1), old=[emoji("👍")], new=[],
        )
        self.assertEqual(await self.runtime.handle(removed), "removed_or_unchanged")
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_non_admin_reaction_is_ignored(self):
        item = update(group_id=-1001, message_id=500, actor_id=99, when=self.now, new=[emoji("👍")])
        self.assertEqual(await self.runtime.handle(item), "ignored_permissions")
        self.bot.send_message.assert_not_awaited()

    async def test_mode2_identified_forwards_source_only_once_across_admins(self):
        await self.db.set_reaction_service_topic(
            channel_id=self.channel_id, topic_id=9000, topic_name="Важное", updated_by=10,
        )
        first = update(group_id=-1001, message_id=500, actor_id=11, when=self.now, new=[emoji("👍")])
        second = update(
            group_id=-1001, message_id=500, actor_id=12,
            when=self.now + timedelta(seconds=2), new=[emoji("🔥")],
        )
        self.assertEqual(await self.runtime.handle(first), "service_dispatched")
        self.assertEqual(await self.runtime.handle(second), "already_dispatched")
        self.bot.forward_message.assert_awaited_once_with(
            chat_id=-1001, message_thread_id=9000, from_chat_id=-1001, message_id=500,
        )
        self.bot.copy_message.assert_not_awaited()
        self.assertFalse(any(call.kwargs.get("chat_id") == 42 for call in self.bot.send_message.await_args_list))

    async def test_mode2_anonymous_copies_and_exposes_only_anonymous_tag(self):
        await self.db.set_privacy_mode(channel_id=self.channel_id, user_id=42, privacy_mode="anonymous")
        tag = await self.db.get_anonymous_tag(channel_id=self.channel_id, user_id=42)
        await self.db.create_topic_mapping(
            channel_id=self.channel_id, user_id=42, privacy_mode="anonymous",
            group_id=-1001, topic_id=78,
        )
        await self.db.record_reaction_source(
            channel_id=self.channel_id, group_id=-1001, forum_message_id=501,
            user_id=42, privacy_mode="anonymous", private_chat_id=42,
            private_message_id=101, topic_id=78,
        )
        await self.db.set_reaction_service_topic(
            channel_id=self.channel_id, topic_id=9000, topic_name="Важное", updated_by=10,
        )
        item = update(group_id=-1001, message_id=501, actor_id=11, when=self.now, new=[emoji("⭐")])
        self.assertEqual(await self.runtime.handle(item), "service_dispatched")
        self.bot.forward_message.assert_not_awaited()
        self.bot.copy_message.assert_awaited_once_with(
            chat_id=-1001, message_thread_id=9000, from_chat_id=-1001, message_id=501,
        )
        admin_texts = [str(call.kwargs.get("text", "")) for call in self.bot.send_message.await_args_list]
        self.assertTrue(any(tag in text for text in admin_texts))
        self.assertFalse(any(call.kwargs.get("chat_id") == 42 for call in self.bot.send_message.await_args_list))

    async def test_service_mode_requires_healthy_topic(self):
        settings = await self.db.get_channel_reaction_settings(self.channel_id)
        self.assertEqual(settings["mode"], "subscriber")
        with self.assertRaises(ValueError):
            await self.db.set_channel_reaction_mode(
                channel_id=self.channel_id, mode="service", updated_by=10,
            )
        await self.db.set_reaction_service_topic(
            channel_id=self.channel_id, topic_id=9000, topic_name="Важное", updated_by=10,
        )
        await self.db.mark_reaction_service_topic_unavailable(channel_id=self.channel_id)
        with self.assertRaises(ValueError):
            await self.db.set_channel_reaction_mode(
                channel_id=self.channel_id, mode="service", updated_by=10,
            )

    async def test_unmapped_general_or_system_message_is_ignored(self):
        item = update(group_id=-1001, message_id=9999, actor_id=11, when=self.now, new=[emoji("👍")])
        self.assertEqual(await self.runtime.handle(item), "ignored_source")
        self.bot.send_message.assert_not_awaited()
        self.bot.forward_message.assert_not_awaited()
        self.bot.copy_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
