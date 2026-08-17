import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "database.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def async_method_source(name: str) -> str:
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"async method not found: {name}")


class SetupStandardSnapshotStaticTests(unittest.TestCase):
    def test_register_channel_snapshots_before_commit(self):
        source = async_method_source("register_channel")
        snapshot_pos = source.index("_snapshot_active_standard_for_channel_locked")
        commit_pos = source.index("await self.conn.commit()", snapshot_pos)
        self.assertLess(snapshot_pos, commit_pos)
        self.assertIn('await self.conn.execute("BEGIN IMMEDIATE")', source)
        self.assertIn("await self.conn.rollback()", source)

    def test_snapshot_is_copied_from_active_standard_revision(self):
        source = async_method_source("_snapshot_active_standard_for_channel_locked")
        self.assertIn("bot_standard_custom_state", source)
        self.assertIn("INSERT INTO channel_custom_revisions", source)
        self.assertIn("setup_snapshot", source)
        self.assertIn("INSERT INTO channel_custom_items", source)
        self.assertIn("FROM bot_standard_custom_items", source)
        self.assertIn("INSERT INTO channel_custom_state", source)
        self.assertIn("customization_audit_log", source)
        self.assertNotIn("commit()", source)

    def test_legacy_migration_fixtures_can_still_register_pre_v23_channels(self):
        source = async_method_source("_custom_pack_foundation_is_active_locked")
        self.assertIn("schema_migrations", source)
        self.assertIn("version=23", source)
        register = async_method_source("register_channel")
        self.assertIn("_custom_pack_foundation_is_active_locked", register)


if __name__ == "__main__":
    unittest.main()
