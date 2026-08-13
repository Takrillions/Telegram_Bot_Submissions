import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from database import CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS, Database, Migration
from release_runtime import (ReleaseMetadata, auto_rollback_allowed, readiness_is_current, read_release_metadata, retain_releases, write_readiness, write_release_metadata)


class ReleaseRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = str(self.root / "data.sqlite3")

    async def asyncTearDown(self): self.temp.cleanup()

    async def test_validate_inspection_does_not_apply_pending_migration(self):
        db = Database(self.db_path)
        await db.init(); await db.close()
        async def add_table(conn): await conn.execute("CREATE TABLE future_marker (id INTEGER)")
        checked = Database(self.db_path, migrations=(*DEFAULT_MIGRATIONS, Migration(2, "future", add_table)))
        pending = await checked.inspect_pending_migrations()
        self.assertEqual([item.version for item in pending], [2])
        self.assertFalse((self.root / "backups").exists())

    async def test_migrate_only_path_applies_pending_and_creates_backup(self):
        db = Database(self.db_path, backup_dir=self.root / "backups")
        await db.init(); await db.close()
        async def add_table(conn): await conn.execute("CREATE TABLE future_marker (id INTEGER)")
        upgraded = Database(self.db_path, backup_dir=self.root / "backups", migrations=(*DEFAULT_MIGRATIONS, Migration(2, "future", add_table)))
        await upgraded.init()
        self.assertEqual(upgraded.applied_migration_versions, (2,))
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)
        await upgraded.close()

    async def test_known_schema_version_comes_from_registry(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS[-1].version)

    async def test_metadata_readiness_rollback_and_retention(self):
        release = self.root / "releases" / "new"; release.mkdir(parents=True)
        metadata = ReleaseMetadata.create("new", "abc", "hash")
        write_release_metadata(release, metadata)
        self.assertEqual(read_release_metadata(release).known_schema_version, CURRENT_SCHEMA_VERSION)
        marker = self.root / "shared" / "runtime" / "readiness.json"
        write_readiness(marker, release_id="new", bot_id=1, bot_username="bot", scheduler_ready=True, polling_ready=True)
        self.assertTrue(readiness_is_current(marker, "new"))
        self.assertFalse(readiness_is_current(marker, "old"))
        self.assertFalse(auto_rollback_allowed(migration_applied=True, previous_known_schema_version=1, database_schema_version=1))
        self.assertTrue(auto_rollback_allowed(migration_applied=False, previous_known_schema_version=1, database_schema_version=1))
        for name in ("old1", "old2", "old3", "old4", "old5"):
            (self.root / "releases" / name).mkdir()
        shared = self.root / "shared"; shared.mkdir(exist_ok=True); (shared / "keep.txt").write_text("x")
        retain_releases(self.root, current_release="new", keep=3)
        self.assertTrue(release.exists())
        self.assertTrue((shared / "keep.txt").exists())


if __name__ == "__main__": unittest.main()
