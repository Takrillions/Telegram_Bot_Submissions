import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup_runtime import create_verified_snapshot, restore_verified_snapshot
from database import CURRENT_SCHEMA_VERSION, DEFAULT_MIGRATIONS, Database, Migration


class PartialMigrationRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_predeploy_snapshot_recovers_from_committed_then_failed_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "data.sqlite3"
            backups = root / "backups"

            baseline = Database(str(db_path), backup_dir=backups)
            await baseline.init()
            await baseline.close()
            snapshot = create_verified_snapshot(db_path, backups, label="pre_deploy")

            async def first(conn):
                await conn.execute("CREATE TABLE committed_before_failure (id INTEGER PRIMARY KEY)")

            async def second(conn):
                await conn.execute("CREATE TABLE rolled_back_failure (id INTEGER PRIMARY KEY)")
                raise RuntimeError("later migration failed")

            upgraded = Database(
                str(db_path),
                backup_dir=backups,
                migrations=(
                    *DEFAULT_MIGRATIONS,
                    Migration(CURRENT_SCHEMA_VERSION + 1, "first_commits", first),
                    Migration(CURRENT_SCHEMA_VERSION + 2, "second_fails", second),
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "later migration failed"):
                await upgraded.init()

            with sqlite3.connect(db_path) as conn:
                versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
                self.assertEqual(versions[-1], CURRENT_SCHEMA_VERSION + 1)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='committed_before_failure'"
                ).fetchone())
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rolled_back_failure'"
                ).fetchone())

            restore_verified_snapshot(snapshot, db_path)

            with sqlite3.connect(db_path) as conn:
                versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
                self.assertEqual(versions[-1], CURRENT_SCHEMA_VERSION)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='committed_before_failure'"
                ).fetchone())
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
