import os
import tempfile
import unittest

from authorization import ChannelAction, ChannelAuthorizer, ChannelRole, permission_matrix
from database import Database


class ChannelAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        await self.db.init()
        _, self.a = await self.db.register_channel(
            owner_id=10, group_id=-10010, group_title="A", default_reset_days=30,
            default_notice_text="notice", default_timezone="UTC",
        )
        _, self.b = await self.db.register_channel(
            owner_id=20, group_id=-10020, group_title="B", default_reset_days=30,
            default_notice_text="notice", default_timezone="UTC",
        )
        self.members = {(-10010, 10), (-10010, 11), (-10020, 20), (-10020, 10)}

        async def member_resolver(group_id, user_id):
            return (group_id, user_id) in self.members

        self.auth = ChannelAuthorizer(db=self.db, member_resolver=member_resolver)

    async def asyncTearDown(self):
        try:
            await self.db.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)

    async def test_owner_is_channel_scoped_and_has_owner_actions(self):
        a_id, b_id = int(self.a["channel_id"]), int(self.b["channel_id"])
        owner = await self.auth.require(actor_id=10, channel_id=a_id, action=ChannelAction.EXPORT)
        ordinary_elsewhere = await self.auth.require(actor_id=10, channel_id=b_id, action=ChannelAction.SUBSCRIBER)
        self.assertEqual(owner.role, ChannelRole.OWNER)
        self.assertTrue(owner.allowed)
        self.assertEqual(ordinary_elsewhere.role, ChannelRole.ADMIN)
        self.assertTrue(ordinary_elsewhere.allowed)
        denied = await self.auth.require(actor_id=10, channel_id=b_id, action=ChannelAction.EXPORT)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "owner_required")

    async def test_current_telegram_admin_is_required_for_ordinary_admin(self):
        channel_id = int(self.a["channel_id"])
        allowed = await self.auth.require(
            actor_id=11, channel_id=channel_id, action=ChannelAction.MODERATION,
            context_group_id=-10010, require_current_telegram_admin=True,
        )
        self.assertEqual(allowed.role, ChannelRole.ADMIN)
        self.members.remove((-10010, 11))
        stale = await self.auth.require(
            actor_id=11, channel_id=channel_id, action=ChannelAction.MODERATION,
            context_group_id=-10010, require_current_telegram_admin=True,
        )
        self.assertFalse(stale.allowed)
        self.assertEqual(stale.reason, "not_current_group_admin")

    async def test_forged_channel_context_and_member_are_denied(self):
        channel_id = int(self.a["channel_id"])
        forged = await self.auth.require(
            actor_id=11, channel_id=channel_id, action=ChannelAction.MODERATION,
            context_group_id=-10020, require_current_telegram_admin=True,
        )
        self.assertFalse(forged.allowed)
        self.assertEqual(forged.reason, "wrong_channel_context")
        member = await self.auth.require(actor_id=99, channel_id=channel_id, action=ChannelAction.SUBSCRIBER)
        self.assertFalse(member.allowed)

    async def test_owner_record_is_not_transferred_when_membership_changes(self):
        channel_id = int(self.a["channel_id"])
        self.members.remove((-10010, 10))
        private_owner = await self.auth.require(actor_id=10, channel_id=channel_id, action=ChannelAction.PANEL)
        stored_owner = await self.auth.resolve_role(actor_id=10, channel_id=channel_id)
        group_action = await self.auth.require(
            actor_id=10, channel_id=channel_id, action=ChannelAction.SETTINGS,
            context_group_id=-10010, require_current_telegram_admin=True,
        )
        ordinary = await self.auth.require(actor_id=11, channel_id=channel_id, action=ChannelAction.PANEL)
        self.assertFalse(private_owner.allowed)
        self.assertEqual(stored_owner.role, ChannelRole.OWNER)
        self.assertFalse(group_action.allowed)
        self.assertEqual(group_action.reason, "owner_no_longer_group_admin")
        self.assertFalse(ordinary.allowed)
        self.assertEqual((await self.db.get_channel_by_id(channel_id))["owner_id"], 10)

    async def test_matrix_is_conservative_and_explicit(self):
        matrix = permission_matrix()
        self.assertIn(ChannelAction.MODERATION, matrix[ChannelRole.ADMIN])
        self.assertNotIn(ChannelAction.EXPORT, matrix[ChannelRole.ADMIN])
        self.assertIn(ChannelAction.EXPORT, matrix[ChannelRole.OWNER])
        self.assertIn(ChannelAction.BROADCAST, matrix[ChannelRole.OWNER])
        self.assertNotIn(ChannelAction.BROADCAST, matrix[ChannelRole.ADMIN])
        self.assertIn(ChannelAction.REACTION_SETTINGS, matrix[ChannelRole.OWNER])
        self.assertNotIn(ChannelAction.REACTION_SETTINGS, matrix[ChannelRole.ADMIN])
        self.assertIn(ChannelAction.REACTION_TRIGGER, matrix[ChannelRole.ADMIN])

