from pathlib import Path
import unittest


class CustomPackFoundationStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path("database.py").read_text(encoding="utf-8")

    def test_schema_version_23_is_registered(self) -> None:
        self.assertIn('Migration(23, "custom_pack_foundation", apply_custom_pack_foundation)', self.source)

    def test_foundation_tables_are_present(self) -> None:
        for table in (
            "bot_standard_custom_revisions",
            "bot_standard_custom_items",
            "bot_standard_custom_state",
            "channel_custom_revisions",
            "channel_custom_items",
            "channel_custom_state",
            "customization_audit_log",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.source)

    def test_channel_state_uses_composite_fk_to_prevent_cross_channel_revision(self) -> None:
        self.assertIn(
            "FOREIGN KEY(active_revision_id, channel_id) REFERENCES channel_custom_revisions(revision_id, channel_id)",
            self.source,
        )
        self.assertIn(
            "FOREIGN KEY(initial_revision_id, channel_id) REFERENCES channel_custom_revisions(revision_id, channel_id)",
            self.source,
        )

    def test_foundation_keeps_legacy_overlay_api_for_compatibility(self) -> None:
        self.assertIn("include_legacy_template_overlay", self.source)
        self.assertIn("channel_template_overrides", self.source)


if __name__ == "__main__":
    unittest.main()
