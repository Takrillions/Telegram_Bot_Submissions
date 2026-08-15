import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup_runtime import (
    BackupError,
    BackupSettings,
    create_verified_snapshot,
    perform_backup,
    restore_verified_snapshot,
    verify_sqlite_database,
)


class FakeRemoteStore:
    def __init__(self):
        self.uploaded = []
        self.keep = None

    def upload(self, local_path: Path) -> str:
        self.uploaded.append(local_path.name)
        return f"gs://private/{local_path.name}"

    def retain(self, keep: int) -> None:
        self.keep = keep


class BackupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "data" / "bot.sqlite3"
        self.db_path.parent.mkdir()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('before')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_is_verified_and_contains_wal_safe_data(self):
        backup = create_verified_snapshot(self.db_path, self.root / "backups", label="daily")
        verify_sqlite_database(backup)
        conn = sqlite3.connect(backup)
        try:
            self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "before")
        finally:
            conn.close()

    def test_restore_replaces_database_with_verified_snapshot(self):
        backup = create_verified_snapshot(self.db_path, self.root / "backups", label="pre_deploy")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE sample SET value='after'")
        conn.commit(); conn.close()
        restore_verified_snapshot(backup, self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "before")
        finally:
            conn.close()

    def test_remote_backup_uploads_and_applies_retention(self):
        settings = BackupSettings(
            database_path=self.db_path,
            backup_dir=self.root / "backups",
            local_keep=3,
            remote_bucket="private-bucket",
            remote_prefix="bot/sqlite",
            remote_keep=9,
        )
        store = FakeRemoteStore()
        result = perform_backup(settings, label="daily", require_remote=True, remote_store=store)
        self.assertTrue(result.local_path.exists())
        self.assertEqual(result.remote_uri, f"gs://private/{result.local_path.name}")
        self.assertEqual(store.uploaded, [result.local_path.name])
        self.assertEqual(store.keep, 9)

    def test_remote_required_fails_closed_when_not_configured(self):
        settings = BackupSettings(
            database_path=self.db_path,
            backup_dir=self.root / "backups",
            local_keep=3,
            remote_bucket="",
            remote_prefix="bot/sqlite",
            remote_keep=9,
        )
        with self.assertRaisesRegex(BackupError, "Remote backup is required"):
            perform_backup(settings, label="daily", require_remote=True)
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
