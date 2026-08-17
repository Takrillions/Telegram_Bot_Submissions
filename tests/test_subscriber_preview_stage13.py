import unittest

from subscriber_preview import (
    SUBSCRIBER_PREVIEW_BY_KEY,
    SUBSCRIBER_PREVIEW_SCENARIOS,
    customization_context_header,
    render_subscriber_preview_scenario,
    subscriber_preview_home_text,
    subscriber_preview_marker,
)
from templates import render_label


class FakePreviewDB:
    def __init__(self):
        self.calls = []
        self.live = {
            "privacy.prompt": "LIVE PRIVACY",
            "message.received": "LIVE RECEIVED",
            "message.channel_unavailable": "LIVE UNAVAILABLE",
            "sanction.applied.visible": "LIVE {action} / {duration} / {reason}",
            "ui.privacy.anonymous": "LIVE ANON",
        }
        self.draft = {
            "privacy.prompt": "DRAFT PRIVACY",
            "message.received": "DRAFT RECEIVED",
            "message.channel_unavailable": "DRAFT UNAVAILABLE",
            "sanction.applied.visible": "DRAFT {action} / {duration} / {reason}",
            "ui.privacy.anonymous": "DRAFT ANON",
        }

    async def get_channel_custom_template_text(
        self,
        *,
        channel_id,
        template_key,
        include_legacy_template_overlay=True,
        include_draft=False,
        revision_id=None,
    ):
        self.calls.append(("get", channel_id, template_key, include_draft))
        if include_draft and template_key in self.draft:
            return self.draft[template_key]
        return self.live.get(template_key)

    async def get_template_override(self, **kwargs):
        self.calls.append(("legacy", kwargs))
        return None


class SubscriberPreviewStage13Tests(unittest.IsolatedAsyncioTestCase):
    def test_required_scenarios_are_present(self):
        self.assertEqual(
            [item.key for item in SUBSCRIBER_PREVIEW_SCENARIOS],
            ["start", "privacy", "received", "unavailable", "sanction", "admin_reply", "cleanup"],
        )
        self.assertEqual(set(SUBSCRIBER_PREVIEW_BY_KEY), {
            "start", "privacy", "received", "unavailable", "sanction", "admin_reply", "cleanup"
        })

    async def test_template_scenarios_always_use_draft_overlay(self):
        db = FakePreviewDB()
        text = await render_subscriber_preview_scenario(
            db=db, channel_id=7, scenario_key="received"
        )
        self.assertEqual(text, "DRAFT RECEIVED")
        self.assertIn(("get", 7, "message.received", True), db.calls)

    async def test_sanction_sample_uses_safe_sample_values(self):
        db = FakePreviewDB()
        text = await render_subscriber_preview_scenario(
            db=db, channel_id=7, scenario_key="sanction"
        )
        self.assertIn("DRAFT Ограничение", text)
        self.assertIn("Нарушение правил сообщества", text)
        self.assertNotIn("{reason}", text)

    async def test_cleanup_preview_matches_real_notice_text_not_template(self):
        db = FakePreviewDB()
        text = await render_subscriber_preview_scenario(
            db=db,
            channel_id=7,
            scenario_key="cleanup",
            notice_text="Через 24 часов <удаление>",
        )
        self.assertEqual(text, "Через 24 часов &lt;удаление&gt;")
        self.assertFalse(any(call[0] == "get" and call[2] == "cleanup.notice" for call in db.calls))

    async def test_preview_button_label_can_use_draft(self):
        db = FakePreviewDB()
        label = await render_label(
            db, 7, "ui.privacy.anonymous", include_draft=True
        )
        self.assertEqual(label, "DRAFT ANON")
        self.assertIn(("get", 7, "ui.privacy.anonymous", True), db.calls)

    def test_owner_headers_are_explicit_about_channel_and_draft(self):
        header = customization_context_header(
            channel_name="Команда <A>",
            channel_id=9,
            active_revision_id=44,
            draft_count=3,
        )
        self.assertIn("Сейчас редактируется", header)
        self.assertIn("Команда &lt;A&gt;", header)
        self.assertIn("channel_id=9", header)
        self.assertIn("черновик", header)
        self.assertIn("№44", header)

        home = subscriber_preview_home_text(
            channel_name="Команда",
            channel_id=9,
            active_revision_id=44,
            draft_count=3,
        )
        self.assertIn("не создаёт подписчика", home)
        self.assertIn("не записывает сообщения/аналитику", home)

        marker = subscriber_preview_marker(
            channel_name="Команда",
            channel_id=9,
            scenario_title="Выбор приватности",
            draft_count=3,
        )
        self.assertIn("ПРЕДПРОСМОТР", marker)
        self.assertIn("черновик + опубликованная версия", marker)


if __name__ == "__main__":
    unittest.main()
