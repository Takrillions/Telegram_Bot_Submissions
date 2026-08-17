from pathlib import Path
import unittest


class TemplateSurfaceMigrationV25StaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path("database.py").read_text(encoding="utf-8")

    def test_v25_is_registered(self):
        self.assertIn(
            'Migration(25, "template_surface_v25", apply_template_surface_v25)',
            self.source,
        )

    def test_v25_creates_successor_standard_revision(self):
        self.assertIn('"schema_v25_defaults"', self.source)
        self.assertIn('"schema_version": 25', self.source)
        self.assertIn("bot_standard_custom_revisions", self.source)
        self.assertIn("bot_standard_custom_items", self.source)

    def test_v25_does_not_mutate_channel_snapshots(self):
        start = self.source.index("async def apply_template_surface_v25")
        end = self.source.index("async def apply_custom_drafts_v26", start)
        body = self.source[start:end]
        self.assertNotIn("UPDATE channel_custom", body)
        self.assertNotIn("INSERT INTO channel_custom", body)
        self.assertNotIn("DELETE FROM channel_custom", body)
        self.assertIn("UPDATE bot_standard_custom_state", body)


if __name__ == "__main__":
    unittest.main()
