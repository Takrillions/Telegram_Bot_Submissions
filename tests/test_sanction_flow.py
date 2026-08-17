import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from database import Database
from templates import TEMPLATE_REGISTRY
from handlers import (
    SanctionFlow,
    SubscriberMetadataFlow,
    apply_sanction_from_flow,
    authorize_sanction_target,
    deliver_rate_limit_notification,
    rate_limit_notification_text,
    sanction_confirmation_text,
    sanction_duration_keyboard,
    sanction_flow_is_complete,
)


class _Guard:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed

    async def is_group_admin(self, message, group_id: int) -> bool:
        return self.allowed


def _message(*, group_id: int, topic_id: int, admin_id: int = 700):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=admin_id),
        chat=SimpleNamespace(id=group_id, type=ChatType.SUPERGROUP),
        message_thread_id=topic_id,
    )


class SanctionFlowFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "bot.sqlite3"))
        await self.db.init()
        _, self.first = await self.db.register_channel(
            owner_id=1, group_id=-1001, group_title="A", default_reset_days=30,
            default_notice_text="notice", default_timezone="UTC",
        )
        _, self.second = await self.db.register_channel(
            owner_id=2, group_id=-1002, group_title="B", default_reset_days=30,
            default_notice_text="notice", default_timezone="UTC",
        )
        self.first_id = int(self.first["channel_id"])
        self.second_id = int(self.second["channel_id"])
        await self.db.upsert_user(user_id=42, first_name="Real name", last_name="Last", username="real_name")
        await self.db.attach_subscriber(channel_id=self.first_id, user_id=42)
        await self.db.create_topic_mapping(
            channel_id=self.first_id, user_id=42, privacy_mode="anonymous", group_id=-1001, topic_id=77,
        )
        self.message = _message(group_id=-1001, topic_id=77)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _state(self):
        storage = MemoryStorage()
        return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=-1001, user_id=700))

    async def test_rate_callback_path_is_fsm_only_and_legacy_callback_is_rejected(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        section = source[source.index('async def subscriber_rate_menu'):source.index('async def subscriber_spam_callback')]
        self.assertIn("SanctionFlow.parameters", section)
        self.assertNotIn("apply_subscriber_sanction(", section)
        self.assertNotIn("update_subscriber_moderation(", section)
        self.assertIn('subscriber:set_rate:', section)
        self.assertIn('callback_flow_text(callback, "invalid_callback")', section)

    async def test_fsm_context_keeps_channel_target_and_no_anonymous_identity(self):
        state = await self._state()
        await state.set_state(SanctionFlow.parameters)
        await state.update_data(
            channel_id=self.first_id, target_user_id=42, privacy_mode="anonymous",
            sanction_type="rate_limit", sanction_parameters={}, reason_choice=None,
            custom_reason=None, show_reason_to_subscriber=None,
        )
        data = await state.get_data()
        self.assertEqual(await state.get_state(), SanctionFlow.parameters.state)
        self.assertEqual((data["channel_id"], data["target_user_id"]), (self.first_id, 42))
        self.assertEqual(data["privacy_mode"], "anonymous")
        self.assertFalse({"first_name", "last_name", "username", "display_name"} & set(data))
        self.assertEqual(SanctionFlow.reason.state, "SanctionFlow:reason")
        self.assertEqual(SanctionFlow.custom_reason.state, "SanctionFlow:custom_reason")
        self.assertEqual(SanctionFlow.visibility.state, "SanctionFlow:visibility")
        self.assertEqual(SanctionFlow.confirmation.state, "SanctionFlow:confirmation")

    async def test_cancel_or_stale_state_clears_without_creating_sanction(self):
        state = await self._state()
        await state.set_state(SanctionFlow.parameters)
        await state.update_data(channel_id=self.first_id, target_user_id=42)
        await state.clear()
        self.assertIsNone(await state.get_state())
        self.assertEqual(await state.get_data(), {})
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))
        self.assertIn("sanction.flow.expired", TEMPLATE_REGISTRY)

    async def test_server_authorization_rejects_forged_channel_and_wrong_target_channel(self):
        guard = _Guard()
        self.assertIsNotNone(await authorize_sanction_target(
            message=self.message, db=self.db, guard=guard, channel_id=self.first_id, user_id=42,
        ))
        self.assertIsNone(await authorize_sanction_target(
            message=self.message, db=self.db, guard=guard, channel_id=self.second_id, user_id=42,
        ))
        self.assertIsNone(await authorize_sanction_target(
            message=self.message, db=self.db, guard=guard, channel_id=self.first_id, user_id=999,
        ))

    async def test_future_finalization_rechecks_admin_rights_and_context(self):
        data = {
            "channel_id": self.first_id,
            "target_user_id": 42,
            "privacy_mode": "anonymous",
            "sanction_type": "rate_limit",
            "sanction_parameters": {"rate_limit_seconds": 300},
            "reason_choice": "spam",
            "custom_reason": None,
            "show_reason_to_subscriber": False,
        }
        denied = await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(False), flow_data=data,
        )
        self.assertIsNone(denied)
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))
        forged = dict(data, channel_id=self.second_id)
        rejected = await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data=forged,
        )
        self.assertIsNone(rejected)
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.second_id, user_id=42))


    def _complete_data(self, **overrides):
        data = {
            "channel_id": self.first_id,
            "target_user_id": 42,
            "privacy_mode": "anonymous",
            "sanction_type": "rate_limit",
            "sanction_parameters": {"rate_limit_seconds": 300},
            "reason_choice": "spam",
            "custom_reason": None,
            "show_reason_to_subscriber": False,
        }
        data.update(overrides)
        return data

    async def test_each_ready_reason_reaches_complete_confirmation_data(self):
        choices = ("spam", "flood", "insult", "rules", "advertising", "suspicious_activity")
        for choice in choices:
            label = Database.resolve_sanction_reason(choice)
            data = self._complete_data(reason_choice=choice)
            self.assertTrue(sanction_flow_is_complete(data))
            self.assertIn(label, sanction_confirmation_text(data, anonymous_tag="Анон-7"))

    async def test_custom_reason_rejects_empty_or_whitespace_and_is_shown_after_valid_input(self):
        self.assertFalse(sanction_flow_is_complete(self._complete_data(reason_choice="other", custom_reason="")))
        self.assertFalse(sanction_flow_is_complete(self._complete_data(reason_choice="other", custom_reason="   ")))
        data = self._complete_data(reason_choice="other", custom_reason="Своя причина")
        self.assertTrue(sanction_flow_is_complete(data))
        self.assertIn("Своя причина", sanction_confirmation_text(data, anonymous_tag="Анон-7"))
        injection = self._complete_data(reason_choice="other", custom_reason="<b>unsafe</b>")
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", sanction_confirmation_text(injection, anonymous_tag="Анон-7"))

    async def test_visibility_is_boolean_and_confirmation_is_anonymous_safe(self):
        visible = self._complete_data(show_reason_to_subscriber=True)
        hidden = self._complete_data(show_reason_to_subscriber=False)
        self.assertTrue(sanction_flow_is_complete(visible))
        self.assertTrue(sanction_flow_is_complete(hidden))
        text = sanction_confirmation_text(visible, anonymous_tag="Анон-7")
        self.assertIn("Анон-7", text)
        self.assertIn("да", text)
        self.assertNotIn("Real name", text)
        self.assertNotIn("real_name", text)
        self.assertNotIn("#42", text)
        self.assertIn("нет", sanction_confirmation_text(hidden, anonymous_tag="Анон-7"))
        self.assertFalse(sanction_flow_is_complete(self._complete_data(show_reason_to_subscriber="yes")))

    async def test_confirmation_supports_all_sanction_actions(self):
        cases = (
            ("rate_limit", {"rate_limit_seconds": 300}),
            ("mute", {"duration_seconds": 600}),
            ("temporary_block", {"duration_seconds": 3600}),
            ("permanent_block", {}),
            ("warning", {}),
        )
        for action, parameters in cases:
            with self.subTest(action=action):
                data = self._complete_data(sanction_type=action, sanction_parameters=parameters)
                self.assertTrue(sanction_flow_is_complete(data))
                text = sanction_confirmation_text(data, anonymous_tag="Anon-7")
                self.assertIn("Anon-7", text)
                self.assertIn("Причина:", text)
                self.assertNotIn("#42", text)

    async def test_rate_limit_supports_custom_interval_and_canonical_callbacks(self):
        custom = self._complete_data(sanction_parameters={"rate_limit_seconds": 120})
        self.assertTrue(sanction_flow_is_complete(custom))
        self.assertIn("2 мин.", sanction_confirmation_text(custom, anonymous_tag="Anon-7"))
        self.assertIn("2 мин.", rate_limit_notification_text(
            event="applied", seconds=120, until=__import__('database').utc_now(),
            reason=Database.resolve_sanction_reason("spam"), show_reason=False,
        ))
        keyboard = await sanction_duration_keyboard(self.db, self.first_id, "rate_limit")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("sanction:param:rate_limit:300", callbacks)
        self.assertIn("sanction:param:rate_limit:custom", callbacks)
        self.assertFalse(any(value and value.startswith("sanction:param:rate:") for value in callbacks))
        self.assertTrue((await sanction_duration_keyboard(self.db, self.first_id, "mute")).inline_keyboard)
        self.assertTrue((await sanction_duration_keyboard(self.db, self.first_id, "temporary_block")).inline_keyboard)

    async def test_custom_rate_limit_finalization_uses_reason_aware_db_api(self):
        data = self._complete_data(sanction_parameters={"rate_limit_seconds": 120})
        reason = await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data=data,
        )
        self.assertEqual(reason, Database.resolve_sanction_reason("spam"))
        state = await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42)
        self.assertEqual(state["rate_limit_seconds"], 120)

    async def test_confirmation_before_apply_does_not_create_a_sanction(self):
        state = await self._state()
        await state.set_state(SanctionFlow.confirmation)
        await state.update_data(**self._complete_data())
        self.assertTrue(sanction_flow_is_complete(await state.get_data()))
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))

    async def test_cancel_clears_every_sanction_step(self):
        for flow_state in (
            SanctionFlow.reason, SanctionFlow.custom_reason,
            SanctionFlow.visibility, SanctionFlow.confirmation,
        ):
            state = await self._state()
            await state.set_state(flow_state)
            await state.update_data(**self._complete_data())
            await state.clear()
            self.assertIsNone(await state.get_state())
            self.assertEqual(await state.get_data(), {})
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))

    async def test_stale_or_corrupted_final_state_cannot_apply(self):
        stale = await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data={},
        )
        self.assertIsNone(stale)
        malformed = self._complete_data(sanction_parameters={"rate_limit_seconds": 1})
        self.assertIsNone(await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data=malformed,
        ))
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))

    async def test_disabled_channel_blocks_finalization(self):
        await self.db.conn.execute("UPDATE channels SET enabled=0 WHERE channel_id=?", (self.first_id,))
        await self.db.conn.commit()
        self.assertIsNone(await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data=self._complete_data(),
        ))
        self.assertIsNone(await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42))


    async def test_rate_limit_notifications_show_or_hide_saved_reason(self):
        for index, visible in enumerate((True, False), start=1):
            await self.db.apply_subscriber_sanction(
                channel_id=self.first_id, user_id=42, admin_id=700,
                action="rate_limit", reason_choice="other", custom_reason="<b>unsafe</b>",
                show_reason_to_subscriber=visible, rate_limit_seconds=300,
            )
            accepted_at = __import__('database').utc_now()
            await self.db.record_message_event(
                channel_id=self.first_id, user_id=42, privacy_mode="anonymous",
                direction="subscriber_to_admin", message_type="text", occurred_at=accepted_at,
                source_chat_id=42, source_message_id=900 + index, conversation_id=77,
            )
            details = await self.db.active_subscriber_restriction_details(
                channel_id=self.first_id, user_id=42, now=accepted_at,
            )
            self.assertIsNotNone(details)
            kind, until, reason, show_reason = details
            self.assertEqual((kind, show_reason), ("rate_limited", visible))
            applied = rate_limit_notification_text(
                event="applied", seconds=300, until=None, reason=reason, show_reason=show_reason,
            )
            active = rate_limit_notification_text(
                event="active", seconds=300, until=until, reason=reason, show_reason=show_reason,
            )
            if visible:
                self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", applied)
                self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", active)
            else:
                self.assertNotIn("unsafe", applied)
                self.assertNotIn("unsafe", active)

    async def test_rate_limit_is_channel_scoped_and_reopens_each_interval(self):
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=700,
            action="rate_limit", reason_choice="spam", show_reason_to_subscriber=True,
            rate_limit_seconds=300,
        )
        # The policy is active, but it must not block the first publication.
        self.assertIsNone(await self.db.active_subscriber_restriction_details(channel_id=self.first_id, user_id=42))
        active_rows = await self.db.list_active_sanctions(channel_id=self.first_id, user_id=42)
        self.assertEqual(len(active_rows), 1)
        self.assertIsNone(active_rows[0]["expires_at"])

        accepted_at = __import__('database').utc_now()
        await self.db.record_message_event(
            channel_id=self.first_id, user_id=42, privacy_mode="anonymous",
            direction="subscriber_to_admin", message_type="text", occurred_at=accepted_at,
            source_chat_id=42, source_message_id=950, conversation_id=77,
        )
        first = await self.db.active_subscriber_restriction_details(
            channel_id=self.first_id, user_id=42, now=accepted_at,
        )
        self.assertEqual(first[0], "rate_limited")
        self.assertIsNone(await self.db.active_subscriber_restriction_details(channel_id=self.second_id, user_id=42))
        later = await self.db.active_subscriber_restriction_details(
            channel_id=self.first_id, user_id=42,
            now=first[1] + __import__('datetime').timedelta(seconds=1),
        )
        self.assertIsNone(later)
        # The rate-limit policy itself remains active after the interval opens.
        self.assertEqual(len(await self.db.list_active_sanctions(channel_id=self.first_id, user_id=42, now=first[1] + __import__('datetime').timedelta(seconds=1))), 1)

    async def test_notification_delivery_failure_keeps_single_saved_sanction(self):
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=700,
            action="rate_limit", reason_choice="spam", show_reason_to_subscriber=True,
            rate_limit_seconds=300,
        )
        class FailingBot:
            async def send_message(self, **kwargs):
                raise TelegramBadRequest(method=object(), message="delivery failed")
        delivered = await deliver_rate_limit_notification(
            bot=FailingBot(), user_id=42, seconds=300,
            until=__import__('database').utc_now(), reason=Database.resolve_sanction_reason("spam"), show_reason=True,
        )
        self.assertFalse(delivered)
        state = await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42)
        self.assertEqual(state["sanction_reason"], Database.resolve_sanction_reason("spam"))
        self.assertEqual(len(await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)), 1)

    async def test_notification_delivery_uses_templates_and_does_not_duplicate_moderation_log(self):
        class RecordingBot:
            def __init__(self): self.calls = []
            async def send_message(self, **kwargs): self.calls.append(kwargs)
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=700,
            action="rate_limit", reason_choice="spam", show_reason_to_subscriber=False,
            rate_limit_seconds=300,
        )
        bot = RecordingBot()
        self.assertTrue(await deliver_rate_limit_notification(
            bot=bot, user_id=42, seconds=300,
            until=__import__('database').utc_now(), reason=Database.resolve_sanction_reason("spam"), show_reason=False,
        ))
        self.assertEqual(len(bot.calls), 1)
        self.assertNotIn(Database.resolve_sanction_reason("spam"), bot.calls[0]["text"])
        self.assertEqual(len(await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)), 1)

    async def test_subscriber_handler_checks_restriction_before_forwarding(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        section = source[source.index('async def subscriber_message_handler'):source.index('async def subscriber_history_handler')]
        self.assertLess(section.index("active_subscriber_restriction_details("), section.index("runtime.accept_user_message("))
        self.assertIn("channel_id=channel_id", section)


    async def test_mute_temp_block_permanent_block_warning_and_priority(self):
        now = __import__('database').utc_now()
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="rate_limit",reason_choice="spam",rate_limit_seconds=300)
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="mute",reason_choice="flood",duration_seconds=600,show_reason_to_subscriber=True)
        details=await self.db.active_subscriber_restriction_details(channel_id=self.first_id,user_id=42,now=now)
        self.assertEqual((details[0],details[2],details[3]),("muted",Database.resolve_sanction_reason("flood"),True))
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="temporary_block",reason_choice="rules",duration_seconds=600)
        self.assertEqual((await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42))[0],"blocked")
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="permanent_block",reason_choice="spam")
        self.assertEqual((await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42))[0],"permanently_blocked")
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="warning",reason_choice="advertising",show_reason_to_subscriber=False)
        self.assertEqual((await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42))[0],"permanently_blocked")
        actions=[row["action"] for row in await self.db.list_moderation_actions(channel_id=self.first_id,user_id=42)]
        self.assertIn("warning",actions)

    async def test_new_sanctions_are_channel_scoped_expire_and_can_be_revoked_without_history_loss(self):
        now=__import__('database').utc_now()
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="mute",reason_choice="spam",duration_seconds=600)
        self.assertIsNone(await self.db.active_subscriber_restriction(channel_id=self.second_id,user_id=42))
        details=await self.db.active_subscriber_restriction_details(channel_id=self.first_id,user_id=42)
        self.assertIsNone(await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42,now=details[1]+__import__('datetime').timedelta(seconds=1)))
        self.assertGreater(await self.db.revoke_active_sanctions(channel_id=self.first_id,user_id=42,admin_id=700),0)
        self.assertIsNone(await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42))
        history=await self.db.list_moderation_actions(channel_id=self.first_id,user_id=42)
        self.assertIn("clear_restrictions",[row["action"] for row in history])
        self.assertGreaterEqual(len(history),2)

    async def test_warning_does_not_block_messages_and_new_action_types_use_single_flow(self):
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action="warning",reason_choice="spam",show_reason_to_subscriber=True)
        self.assertIsNone(await self.db.active_subscriber_restriction(channel_id=self.first_id,user_id=42))
        source=Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('subscriber:action:',source)
        self.assertIn('action in {"mute", "temporary_block"}',source)
        self.assertIn('action in {"permanent_block", "warning"}',source)

    async def test_reapplying_same_restriction_replaces_current_instance_without_resurrection(self):
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=700, action="mute",
            reason_choice="spam", duration_seconds=600,
        )
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=701, action="mute",
            reason_choice="flood", duration_seconds=120,
        )
        active = await self.db.list_active_sanctions(channel_id=self.first_id, user_id=42)
        self.assertEqual(len([row for row in active if row["action"] == "mute"]), 1)
        self.assertEqual(active[0]["reason"], Database.resolve_sanction_reason("flood"))
        history = await self.db.get_subscriber_moderation_history(
            channel_id=self.first_id, user_id=42, limit=10,
        )
        mute_rows = [row for row in history if row["action"] == "mute"]
        self.assertEqual(len(mute_rows), 2)
        self.assertEqual({row["status"] for row in mute_rows}, {"active", "removed"})

    async def test_moderation_history_keeps_applying_and_revoking_admins(self):
        await self.db.apply_subscriber_sanction(
            channel_id=self.first_id, user_id=42, admin_id=700, action="temporary_block",
            reason_choice="rules", duration_seconds=600, show_reason_to_subscriber=True,
        )
        history = await self.db.get_subscriber_moderation_history(
            channel_id=self.first_id, user_id=42, limit=10,
        )
        block = next(row for row in history if row["action"] == "temporary_block")
        self.assertEqual(block["admin_id"], 700)
        self.assertEqual(block["status"], "active")
        self.assertEqual(block["reason"], Database.resolve_sanction_reason("rules"))
        self.assertTrue(block["show_reason_to_subscriber"])

        self.assertEqual(
            await self.db.revoke_active_sanctions(channel_id=self.first_id, user_id=42, admin_id=701),
            1,
        )
        history = await self.db.get_subscriber_moderation_history(
            channel_id=self.first_id, user_id=42, limit=10,
        )
        block = next(row for row in history if row["action"] == "temporary_block")
        self.assertEqual(block["status"], "removed")
        self.assertEqual(block["revoked_by"], 701)
        clear = next(row for row in history if row["action"] == "clear_restrictions")
        self.assertEqual(clear["admin_id"], 701)

    async def test_notes_and_tags_are_scoped_audited_and_preserve_anonymous_privacy(self):
        note_id = await self.db.add_subscriber_note(
            channel_id=self.first_id, user_id=42, admin_id=700, note_text="  внутренняя   заметка  ",
        )
        self.assertGreater(note_id, 0)
        notes = await self.db.list_subscriber_notes(channel_id=self.first_id, user_id=42)
        self.assertEqual(notes[0]["note_text"], "внутренняя заметка")
        self.assertTrue(await self.db.add_subscriber_tag(
            channel_id=self.first_id, user_id=42, admin_id=700, tag="  спам  ",
        ))
        self.assertFalse(await self.db.add_subscriber_tag(
            channel_id=self.first_id, user_id=42, admin_id=700, tag="СПАМ",
        ))
        self.assertEqual([row["tag"] for row in await self.db.list_subscriber_tags(channel_id=self.first_id, user_id=42)], ["спам"])
        self.assertEqual(await self.db.list_subscriber_notes(channel_id=self.second_id, user_id=42), [])
        self.assertEqual(await self.db.list_subscriber_tags(channel_id=self.second_id, user_id=42), [])
        actions = [row["action"] for row in await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)]
        self.assertEqual(actions.count("note_added"), 1)
        self.assertEqual(actions.count("tag_added"), 1)
        self.assertNotIn("внутренняя заметка", str(await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)))

    async def test_metadata_rejects_blank_or_unattached_target_and_has_minimal_anonymous_fsm_data(self):
        for value in ("", "   ", "x" * 1001):
            with self.assertRaises(ValueError):
                await self.db.add_subscriber_note(channel_id=self.first_id, user_id=42, admin_id=700, note_text=value)
        with self.assertRaises(ValueError):
            await self.db.add_subscriber_tag(channel_id=self.second_id, user_id=42, admin_id=700, tag="метка")
        state = await self._state()
        await state.set_state(SubscriberMetadataFlow.note)
        await state.update_data(channel_id=self.first_id, target_user_id=42, privacy_mode="anonymous")
        data = await state.get_data()
        self.assertEqual(await state.get_state(), SubscriberMetadataFlow.note.state)
        self.assertEqual((data["channel_id"], data["target_user_id"]), (self.first_id, 42))
        self.assertFalse({"first_name", "last_name", "username", "telegram_id"} & set(data))
        self.assertIn("subscriber.metadata.access_denied", TEMPLATE_REGISTRY)
        await state.clear()
        self.assertIsNone(await state.get_state())

    async def test_metadata_callbacks_recheck_same_channel_topic_before_writing(self):
        guard = _Guard()
        self.assertIsNotNone(await authorize_sanction_target(
            message=self.message, db=self.db, guard=guard, channel_id=self.first_id, user_id=42,
        ))
        self.assertIsNone(await authorize_sanction_target(
            message=self.message, db=self.db, guard=guard, channel_id=self.second_id, user_id=42,
        ))
        source = Path("handlers.py").read_text(encoding="utf-8")
        section = source[source.index('async def metadata_context'):source.index('async def subscriber_sanction_action')]
        self.assertIn("authorize_sanction_target", section)
        self.assertIn("SubscriberMetadataFlow.note", section)
        self.assertIn("SubscriberMetadataFlow.tag", section)
        self.assertIn('subscriber:meta:cancel', section)

    async def test_notes_support_pagination_update_soft_delete_and_audit_without_text(self):
        ids = []
        for index in range(7):
            ids.append(await self.db.add_subscriber_note(
                channel_id=self.first_id, user_id=42, admin_id=700, note_text=f"note {index}",
            ))
        first_page = await self.db.list_subscriber_notes(channel_id=self.first_id, user_id=42, limit=5)
        second_page = await self.db.list_subscriber_notes(channel_id=self.first_id, user_id=42, offset=5, limit=5)
        self.assertEqual((len(first_page), len(second_page)), (5, 2))
        note_id = ids[0]
        self.assertTrue(await self.db.update_subscriber_note(
            channel_id=self.first_id, user_id=42, note_id=note_id, admin_id=701, note_text="  changed note  ",
        ))
        self.assertEqual((await self.db.get_subscriber_note(channel_id=self.first_id, user_id=42, note_id=note_id))["note_text"], "changed note")
        with self.assertRaises(ValueError):
            await self.db.update_subscriber_note(channel_id=self.first_id, user_id=42, note_id=note_id, admin_id=701, note_text="   ")
        self.assertTrue(await self.db.soft_delete_subscriber_note(
            channel_id=self.first_id, user_id=42, note_id=note_id, admin_id=702,
        ))
        self.assertFalse(await self.db.soft_delete_subscriber_note(
            channel_id=self.first_id, user_id=42, note_id=note_id, admin_id=702,
        ))
        self.assertIsNone(await self.db.get_subscriber_note(channel_id=self.first_id, user_id=42, note_id=note_id))
        self.assertEqual(await self.db.count_subscriber_notes(channel_id=self.first_id, user_id=42), 6)
        raw = await (await self.db.conn.execute("SELECT note_text,deleted_at FROM subscriber_notes WHERE note_id=?", (note_id,))).fetchone()
        self.assertEqual(raw["note_text"], "changed note")
        self.assertIsNotNone(raw["deleted_at"])
        journal = await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)
        actions = [row["action"] for row in journal]
        self.assertIn("note_updated", actions)
        self.assertIn("note_deleted", actions)
        self.assertNotIn("changed note", str(journal))

    async def test_tags_have_stable_ids_pagination_delete_and_channel_isolation(self):
        for value in ("Test", "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
            self.assertTrue(await self.db.add_subscriber_tag(
                channel_id=self.first_id, user_id=42, admin_id=700, tag=value,
            ))
        self.assertFalse(await self.db.add_subscriber_tag(
            channel_id=self.first_id, user_id=42, admin_id=700, tag="TEST",
        ))
        first_page = await self.db.list_subscriber_tags(channel_id=self.first_id, user_id=42, limit=8)
        second_page = await self.db.list_subscriber_tags(channel_id=self.first_id, user_id=42, offset=8, limit=8)
        self.assertEqual((len(first_page), len(second_page)), (8, 1))
        tag_id = int(first_page[0]["tag_id"])
        self.assertIsNone(await self.db.get_subscriber_tag(channel_id=self.second_id, user_id=42, tag_id=tag_id))
        self.assertFalse(await self.db.delete_subscriber_tag(channel_id=self.second_id, user_id=42, tag_id=tag_id, admin_id=700))
        self.assertTrue(await self.db.delete_subscriber_tag(channel_id=self.first_id, user_id=42, tag_id=tag_id, admin_id=700))
        self.assertFalse(await self.db.delete_subscriber_tag(channel_id=self.first_id, user_id=42, tag_id=tag_id, admin_id=700))
        journal = await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42)
        self.assertEqual([row["action"] for row in journal].count("tag_deleted"), 1)

    async def test_metadata_management_callbacks_are_compact_anonymous_safe_and_reauthorize(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        section = source[source.index('async def metadata_context'):source.index('async def subscriber_sanction_action')]
        self.assertIn("authorize_sanction_target", section)
        self.assertIn("SubscriberMetadataFlow.note_edit", section)
        self.assertIn("note_delete_confirmation", section)
        self.assertIn("tag_delete_confirmation", section)
        self.assertIn("subscriber:meta:note:{int(row['note_id'])}", section)
        self.assertNotIn('callback_data=f\"subscriber:meta:tag:{html.escape', section)
        self.assertIn("anonymous_tag", Path("handlers.py").read_text(encoding="utf-8"))
        state = await self._state()
        await state.set_state(SubscriberMetadataFlow.note_delete_confirmation)
        await state.update_data(channel_id=self.first_id, target_user_id=42, note_id=1, privacy_mode="anonymous")
        self.assertEqual(await state.get_state(), SubscriberMetadataFlow.note_delete_confirmation.state)
        await state.clear()
        self.assertEqual(await state.get_data(), {})

    async def test_subscriber_statistics_is_channel_scoped_metadata_only_and_response_aware(self):
        from datetime import timedelta
        now=__import__('database').utc_now()
        await self.db.create_topic_mapping(channel_id=self.first_id,user_id=42,privacy_mode="anonymous",group_id=-1001,topic_id=77)
        await self.db.record_message_event(channel_id=self.first_id,user_id=42,privacy_mode="anonymous",direction="subscriber_to_admin",message_type="text",occurred_at=now-timedelta(minutes=20),source_chat_id=42,source_message_id=100,conversation_id=77)
        await self.db.record_message_event(channel_id=self.first_id,user_id=42,privacy_mode="anonymous",direction="subscriber_to_admin",message_type="photo",occurred_at=now-timedelta(minutes=15),source_chat_id=42,source_message_id=101,conversation_id=77,media_group_id="album")
        await self.db.record_message_event(channel_id=self.first_id,user_id=42,privacy_mode="anonymous",direction="admin_to_subscriber",message_type="text",occurred_at=now-timedelta(minutes=10),source_chat_id=-1001,source_message_id=102,admin_id=700,conversation_id=77)
        # Duplicate update is ignored by the event journal unique key.
        await self.db.record_message_event(channel_id=self.first_id,user_id=42,privacy_mode="anonymous",direction="subscriber_to_admin",message_type="text",occurred_at=now,source_chat_id=42,source_message_id=100,conversation_id=77)
        stats=await self.db.get_subscriber_statistics(channel_id=self.first_id,user_id=42,timezone_name="UTC",now=now)
        self.assertEqual((stats['subscriber_messages'],stats['admin_replies'],stats['conversations'],stats['answered_conversations'],stats['media']['photo']),(2,1,1,1,1))
        self.assertEqual(stats['average_first_response_seconds'],600.0)
        self.assertEqual(stats['median_first_response_seconds'],600.0)
        self.assertEqual(stats['answered_percentage'],100.0)
        await self.db.attach_subscriber(channel_id=self.second_id,user_id=42)
        other=await self.db.get_subscriber_statistics(channel_id=self.second_id,user_id=42,timezone_name="UTC",now=now)
        self.assertEqual(other['subscriber_messages'],0)
        source=Path('handlers.py').read_text(encoding='utf-8')
        section=source[source.index('async def subscriber_statistics'):source.index('async def metadata_context')]
        self.assertIn('get_subscriber_statistics(', section)
        self.assertIn('channel_id=cid', section)
        self.assertNotIn('first_name',section)

    async def test_moderation_history_is_ordered_channel_scoped_and_keeps_removed_events(self):
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action='mute',reason_choice='spam',duration_seconds=600,show_reason_to_subscriber=False)
        await self.db.apply_subscriber_sanction(channel_id=self.first_id,user_id=42,admin_id=700,action='warning',reason_choice='flood',show_reason_to_subscriber=True)
        await self.db.revoke_active_sanctions(channel_id=self.first_id,user_id=42,admin_id=701)
        rows=await self.db.get_subscriber_moderation_history(channel_id=self.first_id,user_id=42)
        mute=next(row for row in rows if row['action']=='mute')
        warning=next(row for row in rows if row['action']=='warning')
        self.assertEqual((mute['status'],mute['reason'],mute['show_reason_to_subscriber']),('removed',Database.resolve_sanction_reason('spam'),0))
        self.assertEqual(warning['status'],'warning')
        self.assertEqual(await self.db.get_subscriber_moderation_history(channel_id=self.second_id,user_id=42),[])
        source=Path('handlers.py').read_text(encoding='utf-8'); section=source[source.index('async def subscriber_restriction_history'):source.index('async def subscriber_statistics')]
        self.assertIn('metadata_context',section); self.assertIn('channel_id=cid',section); self.assertNotIn('first_name',section)

    async def test_apply_transition_prevents_duplicate_callback_from_reusing_confirmation(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        section = source[source.index('async def sanction_apply'):source.index('async def sanction_callback_expected')]
        self.assertLess(section.index("await state.set_state(SanctionFlow.action)"), section.index("apply_sanction_from_flow("))
        self.assertIn("await state.clear()", section)

    async def test_single_finalization_point_uses_reason_aware_db_api_only_after_authorization(self):
        data = self._complete_data(show_reason_to_subscriber=True)
        reason = await apply_sanction_from_flow(
            message=self.message, db=self.db, guard=_Guard(), flow_data=data,
        )
        self.assertEqual(reason, "\u0421\u043f\u0430\u043c")
        moderation = await self.db.get_subscriber_moderation(channel_id=self.first_id, user_id=42)
        self.assertEqual((moderation["rate_limit_seconds"], moderation["sanction_reason"]), (300, "\u0421\u043f\u0430\u043c"))
        log = (await self.db.list_moderation_actions(channel_id=self.first_id, user_id=42))[0]
        self.assertEqual((log["reason"], log["show_reason_to_subscriber"]), ("\u0421\u043f\u0430\u043c", 1))

