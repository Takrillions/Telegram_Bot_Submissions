from pathlib import Path
import unittest

from templates import (
    TEMPLATE_REGISTRY,
    channel_categories,
    channel_specs_for_category,
    render_template,
)


class FakeCustomizationDB:
    def __init__(self, *, overrides=None, snapshots=None):
        self.overrides = overrides or {}
        self.snapshots = snapshots or {}
        self.override_reads = []
        self.snapshot_reads = []

    async def get_template_override(self, *, channel_id: int, template_key: str):
        self.override_reads.append((channel_id, template_key))
        return self.overrides.get((channel_id, template_key))

    async def get_channel_custom_template_text(
        self, *, channel_id: int, template_key: str, include_legacy_template_overlay: bool,
        include_draft: bool = False, revision_id=None,
    ):
        self.snapshot_reads.append((channel_id, template_key, include_legacy_template_overlay, include_draft))
        return self.snapshots.get((channel_id, template_key))


class TemplateSurfaceStage6Tests(unittest.IsolatedAsyncioTestCase):
    async def test_stage7_snapshot_is_live_base_even_if_legacy_override_exists(self):
        db = FakeCustomizationDB(
            overrides={(1, "start.greeting"): "Legacy {channel_name}"},
            snapshots={(1, "start.greeting"): "Snapshot {channel_name}"},
        )
        rendered = await render_template(db, 1, "start.greeting", channel_name="A")
        self.assertEqual(rendered, "Snapshot A")

    async def test_immutable_channel_snapshot_is_real_base_layer(self):
        db = FakeCustomizationDB(
            snapshots={
                (1, "start.greeting"): "Channel A: {channel_name}",
                (2, "start.greeting"): "Channel B: {channel_name}",
            }
        )
        a = await render_template(db, 1, "start.greeting", channel_name="Alpha")
        b = await render_template(db, 2, "start.greeting", channel_name="Beta")
        self.assertEqual(a, "Channel A: Alpha")
        self.assertEqual(b, "Channel B: Beta")
        self.assertNotEqual(a, b)
        self.assertTrue(all(row[3] is False for row in db.snapshot_reads))

    async def test_registry_default_is_only_fallback_after_snapshot(self):
        db = FakeCustomizationDB(
            snapshots={(1, "start.greeting"): "Frozen: {channel_name}"}
        )
        frozen = await render_template(db, 1, "start.greeting", channel_name="A")
        fallback = await render_template(db, 2, "start.greeting", channel_name="B")
        self.assertEqual(frozen, "Frozen: A")
        self.assertEqual(
            fallback,
            TEMPLATE_REGISTRY["start.greeting"].default.format(channel_name="B"),
        )

    async def test_global_specs_ignore_channel_overlay_and_snapshot(self):
        key = "prestart.overview"
        self.assertEqual(TEMPLATE_REGISTRY[key].scope, "global")
        db = FakeCustomizationDB(
            overrides={(1, key): "BAD {description} {media_state}"},
            snapshots={(1, key): "BAD2 {description} {media_state}"},
        )
        rendered = await render_template(
            db, 1, key, description="Global", media_state="нет"
        )
        self.assertNotIn("BAD", rendered)
        self.assertEqual(db.override_reads, [])
        self.assertEqual(db.snapshot_reads, [])

    def test_channel_editor_exposes_no_global_specs(self):
        categories = channel_categories()
        self.assertTrue(categories)
        for category in categories:
            specs = channel_specs_for_category(category)
            self.assertTrue(specs)
            self.assertTrue(all(spec.scope == "channel" for spec in specs))
        exposed = {
            spec.key
            for category in categories
            for spec in channel_specs_for_category(category)
        }
        self.assertNotIn("prestart.overview", exposed)

    def test_stage6_surface_keys_are_channel_scoped(self):
        required = {
            "ui.panel.statistics",
            "ui.cleanup.enable",
            "ui.reaction.mode1",
            "ui.start_card.edit_text",
            "ui.broadcast.send",
            "ui.search.new",
            "ui.status.closed",
            "ui.subscriber.history",
            "ui.template.reset_all",
            "ui.metadata.open_note",
            "ui.privacy.anonymous",
            "ui.sanction.action.mute",
            "ui.statistics.page.overview",
            "statistics.body.overview",
            "subscriber.statistics",
            "subscriber.history.page",
        }
        self.assertEqual(required - set(TEMPLATE_REGISTRY), set())
        self.assertTrue(all(TEMPLATE_REGISTRY[key].scope == "channel" for key in required))

    def test_major_handlers_use_channel_labels_and_structural_templates(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn('await render_label(db, channel_id, "ui.privacy.anonymous")', source)
        self.assertIn('"subscriber.statistics"', source)
        self.assertIn('"subscriber.history.page"', source)
        self.assertIn('await render_label(db, channel_id, "ui.metadata.open_note"', source)
        self.assertIn('await render_label(db, channel_id, "ui.common.save")', source)
        self.assertIn('await statistics_keyboard(db=db, channel_id=', source)


if __name__ == "__main__":
    unittest.main()
