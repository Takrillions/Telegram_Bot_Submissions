from copy import deepcopy
from pathlib import Path
import unittest

from custom_transfer import (
    CUSTOM_PACK_FORMAT,
    CUSTOM_PACK_SCHEMA_VERSION,
    MAX_CUSTOM_PACK_BYTES,
    CustomPackValidationError,
    build_export_document,
    dumps_export_document,
    normalize_import_document,
    parse_and_normalize_import,
    parse_import_bytes,
)
from templates import TEMPLATE_REGISTRY


class CustomTransferFormatStage10Tests(unittest.TestCase):
    def _document(self, *, media=True):
        texts = {
            key: spec.default
            for key, spec in TEMPLATE_REGISTRY.items()
            if spec.scope == "channel"
        }
        return build_export_document(
            channel_id=17,
            channel_title="Source channel",
            revision_id=42,
            source_standard_revision_id=9,
            template_texts=texts,
            media=(
                {"media_type": "photo", "media_file_id": "telegram-file-id"}
                if media else None
            ),
            omitted_unsupported_items=2,
        )

    def test_round_trip_is_versioned_and_preserves_safe_customization(self):
        document = self._document()
        raw = dumps_export_document(document)
        pack = parse_and_normalize_import(raw)
        self.assertEqual(document["format"], CUSTOM_PACK_FORMAT)
        self.assertEqual(document["schema_version"], CUSTOM_PACK_SCHEMA_VERSION)
        self.assertEqual(pack.source_channel_id, 17)
        self.assertEqual(pack.source_revision_id, 42)
        self.assertEqual(pack.media_type, "photo")
        self.assertEqual(pack.media_file_id, "telegram-file-id")
        self.assertEqual(pack.templates["start.greeting"], TEMPLATE_REGISTRY["start.greeting"].default)
        self.assertIn("ui.panel.overview", pack.templates)
        self.assertNotIn("start.greeting", document["templates"])
        self.assertIn("ui.panel.overview", document["display_settings"]["labels"])

    def test_export_schema_has_no_security_or_private_data_fields(self):
        document = self._document()
        forbidden = {
            "owner_id", "group_id", "target_channel_id", "channel_id",
            "bot_token", "token", "authorization", "permissions", "callback_data",
            "subscriber_id", "user_id", "messages", "statistics", "moderation",
            "private_notes", "gcp_credentials",
        }

        def keys(value):
            result = set()
            if isinstance(value, dict):
                for key, item in value.items():
                    result.add(str(key).lower())
                    result |= keys(item)
            elif isinstance(value, list):
                for item in value:
                    result |= keys(item)
            return result

        actual = keys(document)
        # source_channel_id is metadata about where the pack came from; there is
        # intentionally no target channel_id field that an import can apply.
        actual.discard("source_channel_id")
        self.assertTrue(forbidden.isdisjoint(actual), forbidden & actual)

    def test_owner_or_target_channel_injection_is_rejected(self):
        for key, value in (
            ("owner_id", 999),
            ("channel_id", 999),
            ("callback_data", "panel:prestart"),
            ("authorization", {"role": "superadmin"}),
            ("bot_token", "secret"),
        ):
            with self.subTest(key=key):
                document = self._document()
                document[key] = value
                with self.assertRaises(CustomPackValidationError) as ctx:
                    normalize_import_document(document)
                self.assertEqual(ctx.exception.code, "unknown_field")

    def test_global_template_injection_is_rejected(self):
        document = self._document()
        document["templates"]["channel.open_personal_link"] = "GLOBAL"
        document["metadata"]["template_count"] += 1
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "global_template")

    def test_unknown_ui_or_callback_shape_is_rejected(self):
        document = self._document()
        document["display_settings"]["callback_data"] = {"publish": "admin:godmode"}
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_incompatible_schema_version_is_explicit(self):
        document = self._document()
        document["schema_version"] = CUSTOM_PACK_SCHEMA_VERSION + 1
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "unsupported_version")
        self.assertIn(str(CUSTOM_PACK_SCHEMA_VERSION + 1), ctx.exception.message)
        self.assertIn(str(CUSTOM_PACK_SCHEMA_VERSION), ctx.exception.message)

    def test_invalid_media_is_rejected(self):
        document = self._document()
        document["start_card"]["media"]["media_type"] = "document"
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "invalid_media")

    def test_metadata_count_tampering_is_rejected(self):
        document = self._document()
        document["metadata"]["template_count"] += 10
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "count_mismatch")

    def test_oversize_and_non_json_files_are_rejected_before_semantics(self):
        with self.assertRaises(CustomPackValidationError) as ctx:
            parse_import_bytes(b"x" * (MAX_CUSTOM_PACK_BYTES + 1))
        self.assertEqual(ctx.exception.code, "file_too_large")
        with self.assertRaises(CustomPackValidationError) as ctx:
            parse_import_bytes(b"{not-json")
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_duplicate_json_fields_are_rejected(self):
        raw = b'{"format":"a","format":"b"}'
        with self.assertRaises(CustomPackValidationError) as ctx:
            parse_import_bytes(raw)
        self.assertEqual(ctx.exception.code, "duplicate_field")

    def test_no_media_round_trip_is_explicit_none(self):
        document = self._document(media=False)
        pack = parse_and_normalize_import(dumps_export_document(document))
        self.assertIsNone(pack.media_type)
        self.assertIsNone(pack.media_file_id)
        self.assertIsNone(document["start_card"]["media"])

    def test_export_defaults_are_semantically_importable(self):
        # Regression: every channel default included in an export must itself
        # pass the editor validator. This caught panel.overview's accidental
        # optional timezone being marked as required.
        document = self._document()
        normalize_import_document(document)

    def test_supported_telegram_html_survives_import(self):
        document = self._document()
        document["start_card"]["text"] = (
            '<b>{channel_name}</b> '
            '<a href="https://example.com/?a=1&amp;b=2">ссылка</a> '
            '<tg-spoiler>секрет</tg-spoiler> '
            '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> '
            '<tg-time unix="1647531900" format="wDT">время</tg-time> '
            '<blockquote expandable>цитата</blockquote> '
            '<pre><code class="language-python">print(&quot;ok&quot;)</code></pre>'
        )
        pack = normalize_import_document(document)
        self.assertIn('<blockquote expandable>', pack.start_card_text)

    def test_malformed_or_unsupported_telegram_html_is_rejected(self):
        cases = (
            '<b>{channel_name}</i>',
            '<script>{channel_name}</script>',
            '{channel_name} & raw',
            '<a onclick="x">{channel_name}</a>',
            '<blockquote><blockquote>{channel_name}</blockquote></blockquote>',
            '<pre><b>{channel_name}</b></pre>',
        )
        for text in cases:
            with self.subTest(text=text):
                document = self._document()
                document["start_card"]["text"] = text
                with self.assertRaises(CustomPackValidationError) as ctx:
                    normalize_import_document(document)
                self.assertEqual(ctx.exception.code, "invalid_html")

    def test_dynamic_fields_are_rejected_inside_html_attributes(self):
        document = self._document()
        document["start_card"]["text"] = '<a href="https://example.com/{channel_name}">{channel_name}</a>'
        with self.assertRaises(CustomPackValidationError) as ctx:
            normalize_import_document(document)
        self.assertEqual(ctx.exception.code, "invalid_html")


class Stage10StaticBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.database = (root / "database.py").read_text(encoding="utf-8")
        cls.handlers = (root / "handlers.py").read_text(encoding="utf-8")
        cls.templates = (root / "templates.py").read_text(encoding="utf-8")

    def test_schema_and_publication_source(self):
        self.assertIn('Migration(29, "custom_transfer_json", apply_custom_transfer_v29)', self.database)
        self.assertIn('"copy_from_channel", "import"', self.database)

    def test_import_is_staged_not_direct_live_write(self):
        start = self.database.index("async def stage_channel_custom_import")
        end = self.database.index("async def stage_channel_custom_revision_restore", start)
        block = self.database[start:end]
        self.assertIn("_stage_bulk_custom_plan_locked", block)
        self.assertIn('publish_source="import"', block)
        self.assertIn('audit_action="custom_imported"', block)
        self.assertNotIn("UPDATE channel_custom_state", block)

    def test_export_and_import_have_owner_checks_and_audit(self):
        self.assertIn("Customization export requires the channel owner", self.database)
        self.assertIn("Customization import requires the channel owner", self.database)
        self.assertIn("'custom_exported'", self.database)
        self.assertIn('audit_action="custom_imported"', self.database)

    def test_handlers_use_private_json_flow_and_media_get_file_validation(self):
        self.assertIn('callback_data="panel:custom_transfer"', self.handlers)
        self.assertIn("CustomTransferFlow.import_file", self.handlers)
        self.assertIn("CustomTransferFlow.import_confirmation", self.handlers)
        self.assertIn("MAX_CUSTOM_PACK_BYTES", self.handlers)
        self.assertIn("await bot.get_file(pack.media_file_id)", self.handlers)
        self.assertIn("stage_channel_custom_import", self.handlers)
        self.assertIn("custom_transfer_staged_keyboard", self.handlers)

    def test_confirmation_validation_error_is_not_put_in_callback_alert(self):
        start = self.handlers.index('if action == "confirm_import":')
        end = self.handlers.index('await state.clear()\n            await callback.message.edit_text(', start)
        block = self.handlers[start:end]
        self.assertIn('await callback.message.answer(', block)
        self.assertNotIn('render_template(db, channel_id, "custom.transfer_invalid", error=error),\n                    show_alert=True', block)

    def test_new_owner_ui_is_channel_scoped(self):
        self.assertIn('"ui.panel.custom_transfer"', self.templates)
        self.assertIn('"custom.transfer_overview"', self.templates)
        self.assertIn('"custom.transfer_invalid"', self.templates)
        self.assertIn('"custom.transfer_media_unavailable"', self.templates)
