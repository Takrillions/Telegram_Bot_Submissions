import os
from pathlib import Path
import tempfile
import unittest

from database import Database
from templates import TEMPLATE_REGISTRY, categories, preview_values, render_default, render_template, validate_template
from handlers import render_statistics_page, trusted_active_channel_for_user


class TemplateRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = Database(self.path)
        await self.db.init()
        _, self.a = await self.db.register_channel(owner_id=1, group_id=-1001, group_title="A", default_reset_days=30, default_notice_text="n", default_timezone="UTC")
        _, self.b = await self.db.register_channel(owner_id=2, group_id=-1002, group_title="B", default_reset_days=30, default_notice_text="n", default_timezone="UTC")

    async def asyncTearDown(self):
        try:
            await self.db.close()
        finally:
            if os.path.exists(self.path): os.unlink(self.path)

    def test_registry_is_complete_structured_and_has_feminine_defaults(self):
        self.assertEqual(len(TEMPLATE_REGISTRY), len(set(TEMPLATE_REGISTRY)))
        self.assertGreaterEqual(len(categories()), 8)
        for spec in TEMPLATE_REGISTRY.values():
            self.assertTrue(spec.default and spec.category and spec.description and spec.used_in and spec.audience)
            masculine_forms = ("\u042f \u043f\u043e\u043b\u0443\u0447\u0438\u043b", "\u042f \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u043b", "\u042f \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u043b", "\u042f \u043d\u0435 \u0441\u043c\u043e\u0433")
            self.assertFalse(any((word + suffix).lower() in spec.default.lower() for word in masculine_forms for suffix in (" ", ".", "!", "\n")))

    async def test_override_is_channel_scoped_and_reset_uses_default(self):
        key = "start.greeting"; a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        await self.db.set_template_override(channel_id=a, template_key=key, custom_text="Спасибо, {channel_name}!", updated_by=1)
        self.assertEqual(await render_template(self.db, a, key, channel_name="A"), "Спасибо, A!")
        self.assertIn("Добро пожаловать", await render_template(self.db, b, key, channel_name="B"))
        self.assertTrue(await self.db.reset_template_override(channel_id=a, template_key=key))
        self.assertIn("Добро пожаловать", await render_template(self.db, a, key, channel_name="A"))

    async def test_reset_all_and_invalid_override_fail_safe(self):
        a = int(self.a["channel_id"])
        await self.db.set_template_override(channel_id=a, template_key="start.greeting", custom_text="<b>{channel_name}</b>", updated_by=1)
        await self.db.set_template_override(channel_id=a, template_key="search.empty", custom_text="Ничего", updated_by=1)
        self.assertEqual(await self.db.reset_all_template_overrides(channel_id=a), 2)
        await self.db.set_template_override(channel_id=a, template_key="start.greeting", custom_text="{unknown}", updated_by=1)
        self.assertIn("Добро пожаловать", await render_template(self.db, a, "start.greeting", channel_name="A"))

    def test_validation_rejects_unsafe_or_malformed_placeholders(self):
        validate_template("start.greeting", "Привет, {channel_name}")
        for value in ("{unknown}", "{channel_name.__class__}", "{channel_name[0]}", "{channel_name", ""):
            with self.assertRaises(ValueError): validate_template("start.greeting", value)

    async def test_render_escapes_runtime_values_and_preview_is_anonymous_safe(self):
        a = int(self.a["channel_id"])
        rendered = await render_template(self.db, a, "start.greeting", channel_name="<img>")
        self.assertIn("&lt;img&gt;", rendered)
        values = preview_values(TEMPLATE_REGISTRY["sanction.rate.applied.visible"])
        self.assertNotIn("user_id", values)
        self.assertNotIn("username", values)



    @staticmethod
    def _statistics_fixture(*, complete: bool = True, anonymous_only: bool = False):
        top = [{"display_name": "\u0410\u043d\u043e\u043d-18", "message_count": 5}]
        if not anonymous_only:
            top.append({"display_name": "\u0418\u043c\u044f \u0430\u0434\u043c\u0438\u043d\u0430", "message_count": 3})
        return {
            "period": "7d", "conversation_metrics_complete": complete,
            "unique_subscribers": 2, "active_subscribers_1d": 1,
            "active_subscribers_7d": 2, "active_subscribers_30d": 2,
            "new_subscribers": 1, "subscriber_messages": 8, "admin_replies": 3,
            "average_messages_per_subscriber": 4.0, "conversation_count": 2,
            "answered_conversation_count": 1, "answered_conversation_share": 50.0,
            "average_first_response_seconds": 60, "median_first_response_seconds": 60,
            "media": {"text": 1, "photo": 0, "video": 0, "document": 0, "voice": 0, "audio": 0, "sticker": 0, "other": 0},
            "album_count": 0, "media_items_count": 0,
            "messages_by_hour": {hour: (1 if hour == 9 else 0) for hour in range(24)},
            "messages_by_weekday": {day: (1 if day == 0 else 0) for day in range(7)},
            "most_active_hour": 9, "most_active_weekday": 0,
            "top_subscribers": top,
            "admins": [], "active_admin_count": 0,
            "team_average_first_response_seconds": None, "team_median_first_response_seconds": None,
            "top_reply_admin": None, "top_first_response_admin": None,
        }

    async def test_statistics_pages_use_effective_channel_overrides_and_legacy_fallback(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        stats = self._statistics_fixture(complete=False)
        for page, key in (
            ("overview", "statistics.page.overview"),
            ("messages", "statistics.page.messages"),
            ("responses", "statistics.page.responses"),
            ("activity", "statistics.page.activity"),
            ("top", "statistics.page.top"),
            ("admins", "statistics.admins"),
        ):
            await self.db.set_template_override(
                channel_id=a, template_key=key,
                custom_text="[A] {page_title} {period} {body}{legacy_warning}",
                updated_by=1,
            )
            rendered_a = await render_statistics_page(db=self.db, channel_id=a, stats=stats, page=page)
            rendered_b = await render_statistics_page(db=self.db, channel_id=b, stats=stats, page=page)
            self.assertIn("[A]", rendered_a)
            self.assertNotIn("[A]", rendered_b)
        await self.db.set_template_override(channel_id=a, template_key="statistics.legacy_warning", custom_text="[LEGACY-A]", updated_by=1)
        self.assertIn("[LEGACY-A]", await render_statistics_page(db=self.db, channel_id=a, stats=stats, page="overview"))
        empty_stats = dict(stats, subscriber_messages=0, top_subscribers=[])
        await self.db.set_template_override(channel_id=a, template_key="statistics.no_data", custom_text="[EMPTY-A]", updated_by=1)
        self.assertIn("[EMPTY-A]", await render_statistics_page(db=self.db, channel_id=a, stats=empty_stats, page="overview"))
        await self.db.set_template_override(channel_id=a, template_key="statistics.unavailable", custom_text="[UNAVAILABLE-A]", updated_by=1)
        self.assertEqual(await render_template(self.db, a, "statistics.unavailable"), "[UNAVAILABLE-A]")
        self.assertNotIn("[UNAVAILABLE-A]", render_default("statistics.unavailable", {}))
        await self.db.set_template_override(channel_id=a, template_key="statistics.page.overview", custom_text="{unknown}", updated_by=1)
        rendered = await render_statistics_page(db=self.db, channel_id=a, stats=stats, page="overview")
        self.assertNotIn("{unknown}", rendered)
        self.assertIn("\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430", rendered)

    async def test_statistics_top_template_context_stays_anonymous_safe(self):
        a = int(self.a["channel_id"])
        stats = self._statistics_fixture(anonymous_only=True)
        await self.db.set_template_override(
            channel_id=a, template_key="statistics.page.top",
            custom_text="{page_title} {period} {body}", updated_by=1,
        )
        rendered = await render_statistics_page(db=self.db, channel_id=a, stats=stats, page="top")
        self.assertIn("\u0410\u043d\u043e\u043d-18", rendered)
        for forbidden in ("\u0418\u0432\u0430\u043d\u043e\u0432", "username", "user_id", "tg://user"):
            self.assertNotIn(forbidden, rendered)

    async def test_export_templates_are_scoped_fail_safe_and_used_by_handler(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        keys = ("export.choose_format", "export.preparing", "export.sent", "export.too_large",
                "export.failed", "export.delivery_failed", "export.unavailable")
        for key in keys:
            await self.db.set_template_override(channel_id=a, template_key=key, custom_text="[A] " + key, updated_by=1)
            self.assertEqual(await render_template(self.db, a, key), "[A] " + key)
            self.assertNotIn("[A]", await render_template(self.db, b, key))
        await self.db.set_template_override(channel_id=a, template_key="export.unavailable", custom_text="[UNAVAILABLE-A]", updated_by=1)
        self.assertEqual(await render_template(self.db, a, "export.unavailable"), "[UNAVAILABLE-A]")
        self.assertNotIn("[UNAVAILABLE-A]", render_default("export.unavailable", {}))
        await self.db.set_template_override(channel_id=a, template_key="export.preparing", custom_text="{unknown}", updated_by=1)
        self.assertNotIn("{unknown}", await render_template(self.db, a, "export.preparing"))
        handler_source = Path("handlers.py").read_text(encoding="utf-8")
        for key in keys:
            self.assertIn(f'"{key}"', handler_source)



    async def test_panel_and_settings_templates_are_scoped_and_fail_safe(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        values = dict(channel_name="A", subscribers=3, topics=2, period_days=30,
                      timezone="UTC", next_reset="14.08.2026 12:00",
                      deep_link="https://t.me/example", notice_text="Notice")
        await self.db.set_template_override(
            channel_id=a, template_key="panel.overview",
            custom_text="[A PANEL] {channel_name} {subscribers} {topics} {period_days} {timezone} {next_reset} {deep_link} {notice_text}",
            updated_by=1,
        )
        self.assertIn("[A PANEL]", await render_template(self.db, a, "panel.overview", **values))
        self.assertNotIn("[A PANEL]", await render_template(self.db, b, "panel.overview", **values))
        for key in ("settings.period_usage", "settings.period_saved", "settings.notice_usage",
                    "settings.notice_too_long", "settings.notice_saved", "settings.topic_template_usage",
                    "settings.topic_template_invalid", "settings.topic_template_saved",
                    "settings.timezone_usage", "settings.timezone_invalid", "settings.timezone_saved",
                    "cleanup.overview", "cleanup.enable_prompt", "cleanup.manual_preview", "cleanup.manual_complete"):
            spec = TEMPLATE_REGISTRY[key]
            rendered = await render_template(self.db, a, key, **preview_values(spec))
            self.assertTrue(rendered)
        await self.db.set_template_override(channel_id=a, template_key="panel.overview", custom_text="{unknown}", updated_by=1)
        fallback = await render_template(self.db, a, "panel.overview", **values)
        self.assertIn("\u041f\u0430\u043d\u0435\u043b\u044c", fallback)
        self.assertNotIn("\u0427\u0430\u0441\u043e\u0432\u043e\u0439 \u043f\u043e\u044f\u0441:", fallback)
        self.assertNotIn("{unknown}", fallback)
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('await _panel_callback_channel(callback, authorizer', source)
        self.assertIn('render_default("panel.unavailable", {})', source)
        self.assertNotIn("anonymous_tag=", source[source.index('async def _panel_text'):source.index('async def authorize_sanction_target')])


    async def test_setup_templates_use_global_defaults_before_channel_and_overrides_after(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        self.assertEqual(TEMPLATE_REGISTRY["setup.supergroup_required"].scope, "global")
        self.assertNotIn("[A]", render_default("setup.supergroup_required", {}))
        await self.db.set_template_override(
            channel_id=a, template_key="setup.success.created",
            custom_text="[A SETUP] {channel_name} {deep_link} {warning}", updated_by=1,
        )
        values = {"channel_name": "<A>", "deep_link": "https://t.me/<bot>?start=ref_c_1", "warning": ""}
        rendered_a = await render_template(self.db, a, "setup.success.created", **values)
        rendered_b = await render_template(self.db, b, "setup.success.created", **values)
        self.assertIn("[A SETUP]", rendered_a)
        self.assertNotIn("[A SETUP]", rendered_b)
        self.assertIn("&lt;A&gt;", rendered_a)
        self.assertIn("&lt;bot&gt;", rendered_a)
        await self.db.set_template_override(channel_id=a, template_key="setup.success.created", custom_text="{unknown}", updated_by=1)
        fallback = await render_template(self.db, a, "setup.success.created", **values)
        self.assertIn("\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u043a\u0430", fallback)
        source = Path("handlers.py").read_text(encoding="utf-8")
        for marker in ("owner_channel_limit", "group_has_other_owner", "ref_c_{channel_id}",
                       'render_default("setup.channel_limit"', 'render_default("setup.owner_conflict"',
                       '"setup.success.created"', '"setup.success.existing"',
                       '"setup.deep_link_invalid"', '"setup.deep_link_unavailable"'):
            self.assertIn(marker, source)


    async def test_channels_and_privacy_templates_are_scoped_and_anonymous_safe(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        user_id = 77
        await self.db.upsert_user(user_id=user_id, first_name="Real Name", last_name=None, username="username")
        await self.db.attach_subscriber(channel_id=a, user_id=user_id)
        await self.db.attach_subscriber(channel_id=b, user_id=user_id)
        self.assertTrue(await self.db.set_active_channel(user_id=user_id, channel_id=a))
        self.assertEqual(
            int((await trusted_active_channel_for_user(self.db, user_id))["channel_id"]), a
        )
        self.assertTrue(await self.db.set_active_channel(user_id=user_id, channel_id=b))
        self.assertEqual(
            int((await trusted_active_channel_for_user(self.db, user_id))["channel_id"]), b
        )
        self.assertEqual(TEMPLATE_REGISTRY["channel.no_available"].scope, "global")
        self.assertEqual(TEMPLATE_REGISTRY["privacy.no_active_channel"].scope, "global")

        await self.db.set_template_override(
            channel_id=a, template_key="channel.selected",
            custom_text="[A CHANNEL] {channel_name}", updated_by=1,
        )
        self.assertEqual(
            await render_template(self.db, a, "channel.selected", channel_name="A"),
            "[A CHANNEL] A",
        )
        self.assertNotIn(
            "[A CHANNEL]",
            await render_template(self.db, b, "channel.selected", channel_name="B"),
        )

        tag = await self.db.set_privacy_mode(
            channel_id=a, user_id=user_id, privacy_mode="anonymous"
        )
        self.assertTrue(tag)
        await self.db.set_template_override(
            channel_id=a, template_key="privacy.current_anonymous",
            custom_text="[A PRIVATE] {anonymous_tag}", updated_by=1,
        )
        rendered = await render_template(
            self.db, a, "privacy.current_anonymous", anonymous_tag=tag
        )
        self.assertIn("[A PRIVATE]", rendered)
        self.assertIn(str(tag), rendered)
        for forbidden in ("Real Name", "@username", str(user_id), "tg://user"):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn(
            "[A PRIVATE]",
            await render_template(self.db, b, "privacy.current_anonymous", anonymous_tag="Анон-19"),
        )

        await self.db.set_template_override(
            channel_id=a, template_key="privacy.switched_anonymous",
            custom_text="{unknown}", updated_by=1,
        )
        fallback = await render_template(
            self.db, a, "privacy.switched_anonymous", anonymous_tag=tag
        )
        self.assertIn("Анонимный режим включён", fallback)
        self.assertNotIn("{unknown}", fallback)

        source = Path("handlers.py").read_text(encoding="utf-8")
        for marker in (
            "trusted_active_channel_for_user",
            'render_default("channel.no_available", {})',
            'render_default("channel.unavailable", {})',
            '"channel.choose_current"',
            '"privacy.current_anonymous"',
            '"privacy.switched_anonymous"',
            '"privacy.already_identified"',
            'callback_data="privacy:anonymous"',
        ):
            self.assertIn(marker, source)
    async def test_status_templates_are_scoped_and_registered(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        keys = (
            "status.context_required", "status.unavailable", "status.overview",
            "status.usage", "status.changed", "status.protection_changed",
        )
        for key in keys:
            self.assertIn(key, TEMPLATE_REGISTRY)
            spec = TEMPLATE_REGISTRY[key]
            rendered = render_default(key, preview_values(spec)) if spec.scope == "global" else await render_template(self.db, a, key, **preview_values(spec))
            self.assertTrue(rendered)
        await self.db.set_template_override(
            channel_id=a, template_key="status.changed",
            custom_text="[A STATUS] {status}", updated_by=1,
        )
        self.assertIn("[A STATUS]", await render_template(self.db, a, "status.changed", status="В работе"))
        self.assertNotIn("[A STATUS]", await render_template(self.db, b, "status.changed", status="В работе"))
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('callback_data="topic:status:new"', source)
        self.assertIn('callback_data="topic:protect:important:toggle"', source)
        self.assertIn('callback_data="topic:protect:pinned:toggle"', source)

    async def test_anonymous_settings_and_subscriber_cards_are_channel_scoped(self):
        a, b = int(self.a["channel_id"]), int(self.b["channel_id"])
        for key in (
            "settings.anonymous_overview", "settings.anonymous_edit_prompt",
            "settings.anonymous_invalid", "settings.anonymous_saved",
            "settings.anonymous_private_required", "subscriber.card.identified",
            "subscriber.card.anonymous", "cleanup.manual_complete_reset",
            "cleanup.manual_reset_skipped",
        ):
            self.assertIn(key, TEMPLATE_REGISTRY)
            self.assertTrue(await render_template(self.db, a, key, **preview_values(TEMPLATE_REGISTRY[key])))

        await self.db.set_template_override(
            channel_id=a, template_key="subscriber.card.anonymous",
            custom_text="[A CARD] {anonymous_tag} {message_count} {last_seen}", updated_by=1,
        )
        values = {"anonymous_tag": "Анон-7", "message_count": 3, "last_seen": "14.08.2026 12:00"}
        self.assertIn("[A CARD]", await render_template(self.db, a, "subscriber.card.anonymous", **values))
        self.assertNotIn("[A CARD]", await render_template(self.db, b, "subscriber.card.anonymous", **values))
        for forbidden in ("first_name", "username", "user_id", "tg://user"):
            self.assertNotIn(forbidden, TEMPLATE_REGISTRY["subscriber.card.anonymous"].variables)

        source = Path("handlers.py").read_text(encoding="utf-8")
        for marker in (
            'callback_data="panel:anonymous"', 'callback_data="panel:anonymous:edit"',
            'ChannelSettingsFlow.anonymous_prefix', 'set_channel_anonymous_prefix',
            'reset_anonymous_cycle', 'ensure_anonymous_tag', 'render_topic_card',
        ):
            self.assertIn(marker, source)

