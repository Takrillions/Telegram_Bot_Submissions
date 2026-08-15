import sqlite3

import aiosqlite
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from database import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_MIGRATIONS,
    Database,
    DatabaseBackupError,
    DatabaseMigrationError,
    DatabasePreflightError,
    Migration,
    SQLiteBackupManager,
    apply_legacy_schema,
)


class DatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "bot.sqlite3")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _create_legacy_database(self, *, with_data: bool = False) -> None:
        conn = await __import__("aiosqlite").connect(self.db_path)
        await apply_legacy_schema(conn)
        if with_data:
            timestamp = "2026-01-01T00:00:00+00:00"
            await conn.execute(
                """
                INSERT INTO tenants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1, -1001, "Legacy group", timestamp, timestamp, 30,
                 "Legacy notice", "Asia/Tashkent", timestamp, 1),
            )
            await conn.execute(
                """
                INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (2, "Legacy", None, "legacy_user", timestamp, timestamp, 0),
            )
            await conn.execute(
                "INSERT INTO tenant_subscribers VALUES (?, ?, ?, ?)",
                (1, 2, timestamp, timestamp),
            )
            await conn.execute(
                "INSERT INTO active_tenant VALUES (?, ?, ?)",
                (2, 1, timestamp),
            )
            await conn.execute(
                "INSERT INTO topics VALUES (?, ?, ?, ?, ?, ?)",
                (1, 2, -1001, 17, timestamp, timestamp),
            )
            await conn.execute(
                "INSERT INTO notification_log VALUES (?, ?, ?, ?)",
                (1, timestamp, 2, timestamp),
            )
        await conn.commit()
        await conn.close()

    async def _migration_versions(self) -> list[int]:
        conn = sqlite3.connect(self.db_path)
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        finally:
            conn.close()

    async def test_empty_database_receives_baseline_schema(self) -> None:
        db = Database(self.db_path, migrations=DEFAULT_MIGRATIONS[:1])
        await db.init()
        self.assertEqual(await self._migration_versions(), [1])
        tables = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        self.assertTrue(
            {"tenants", "users", "topics", "notification_log", "schema_migrations"}
            <= {row["name"] for row in await tables.fetchall()}
        )
        await db.run_preflight()
        await db.close()

    async def test_legacy_schema_is_registered_without_recreating_database(self) -> None:
        await self._create_legacy_database()
        db = Database(self.db_path)
        await db.init()
        self.assertEqual(await self._migration_versions(), list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        await db.run_preflight()
        await db.close()

    async def test_repeated_initialization_is_idempotent(self) -> None:
        first = Database(self.db_path)
        await first.init()
        applied_at = await first.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        )
        first_applied_at = (await applied_at.fetchone())["applied_at"]
        await first.close()

        second = Database(self.db_path)
        await second.init()
        repeated_at = await second.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        )
        self.assertEqual((await repeated_at.fetchone())["applied_at"], first_applied_at)
        self.assertEqual(await self._migration_versions(), list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        await second.close()

    async def test_failed_migration_rolls_back_and_is_not_recorded(self) -> None:
        async def fail_after_write(conn) -> None:
            await conn.execute("CREATE TABLE rollback_probe (id INTEGER)")
            raise RuntimeError("intentional migration failure")

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
                Migration(CURRENT_SCHEMA_VERSION + 1, "intentional_failure", fail_after_write),
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "intentional migration failure"):
            await db.init()

        self.assertEqual(await self._migration_versions(), list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        conn = sqlite3.connect(self.db_path)
        try:
            probe = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rollback_probe'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(probe)

    async def test_legacy_data_and_notification_history_are_preserved(self) -> None:
        await self._create_legacy_database(with_data=True)
        db = Database(self.db_path)
        await db.init()
        tenant = await db.conn.execute("SELECT * FROM channels WHERE owner_id = 1")
        user = await db.conn.execute("SELECT * FROM users WHERE user_id = 2")
        topic = await db.conn.execute("SELECT * FROM channel_topics WHERE channel_id = (SELECT channel_id FROM legacy_owner_channels WHERE owner_id = 1) AND user_id = 2")
        notices = await db.conn.execute("SELECT COUNT(*) AS count FROM channel_notification_log")
        self.assertEqual((await tenant.fetchone())["group_title"], "Legacy group")
        self.assertEqual((await user.fetchone())["username"], "legacy_user")
        self.assertEqual((await topic.fetchone())["topic_id"], 17)
        self.assertEqual((await notices.fetchone())["count"], 1)
        await db.run_preflight()
        await db.close()

    async def test_new_reset_cycles_do_not_delete_notification_history(self) -> None:
        await self._create_legacy_database(with_data=True)
        db = Database(self.db_path)
        await db.init()
        await db.set_channel_period(1, 31)
        await db.advance_channel_reset(
            channel_id=1,
            next_reset_at=datetime.now(timezone.utc),
        )
        notices = await db.conn.execute(
            "SELECT COUNT(*) AS count FROM channel_notification_log"
        )
        self.assertEqual((await notices.fetchone())["count"], 1)
        await db.close()

    async def test_registry_with_gap_is_rejected(self) -> None:
        async def noop(conn) -> None:
            return None

        with self.assertRaises(ValueError):
            Database(
                self.db_path,
                migrations=(
                    Migration(1, "baseline", noop),
                    Migration(18, "gap", noop),
                ),
            )

    async def test_migration_history_with_gap_is_rejected(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                [
                    (1, "baseline_legacy_schema", "2026-01-01T00:00:00+00:00"),
                    (3, "third", "2026-01-01T00:00:00+00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        async def noop(conn) -> None:
            return None

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
                Migration(CURRENT_SCHEMA_VERSION + 1, "third", noop),
            ),
        )
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_migration_history_starting_after_version_one_is_rejected(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (2, "second", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        async def noop(conn) -> None:
            return None

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
            ),
        )
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_unknown_newer_database_migration_is_rejected(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (4, "future_migration", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        db = Database(self.db_path)
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_migration_history_with_mismatched_name_is_rejected(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (1, "renamed_baseline", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        db = Database(self.db_path)
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_malformed_partial_legacy_schema_is_rejected(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE tenants (owner_id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        db = Database(self.db_path)
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_legacy_schema_missing_critical_index_is_rejected(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP INDEX idx_topics_group_topic")
            conn.commit()
        finally:
            conn.close()

        db = Database(self.db_path)
        with self.assertRaises(DatabaseMigrationError):
            await db.init()

    async def test_post_migration_preflight_failure_rolls_back(self) -> None:
        initial = Database(self.db_path)
        await initial.init()
        await initial.close()

        async def create_deferred_foreign_key_violation(conn) -> None:
            await conn.execute("PRAGMA defer_foreign_keys = ON")
            await conn.execute(
                "INSERT INTO channel_subscribers VALUES (?, ?, ?, ?)",
                (999, 888, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
                Migration(CURRENT_SCHEMA_VERSION + 1, "deferred_foreign_key_violation", create_deferred_foreign_key_violation),
            ),
        )
        with self.assertRaises(DatabasePreflightError):
            await db.init()

        self.assertEqual(await self._migration_versions(), list(range(1, CURRENT_SCHEMA_VERSION + 1)))
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM channel_subscribers"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def _backup_dir(self) -> Path:
        return Path(self.temp_dir.name) / "backups"

    def _backup_files(self) -> list[Path]:
        backup_dir = self._backup_dir()
        return sorted(backup_dir.glob("*.sqlite3")) if backup_dir.exists() else []

    async def test_legacy_baseline_creates_backup_before_marking_migration(self) -> None:
        await self._create_legacy_database(with_data=True)
        db = Database(self.db_path, backup_dir=self._backup_dir())
        await db.init()
        backups = self._backup_files()
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(
                backup.execute("SELECT group_title FROM tenants WHERE owner_id = 1").fetchone()[0],
                "Legacy group",
            )
            self.assertIsNone(
                backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            )
        finally:
            backup.close()
        await db.close()

    async def test_migrated_database_without_pending_migrations_creates_no_backup(self) -> None:
        await self._create_legacy_database()
        first = Database(self.db_path, backup_dir=self._backup_dir())
        await first.init()
        await first.close()
        first_backups = [backup.name for backup in self._backup_files()]

        second = Database(self.db_path, backup_dir=self._backup_dir())
        await second.init()
        await second.close()
        self.assertEqual([backup.name for backup in self._backup_files()], first_backups)

    async def test_pending_future_migration_creates_backup_before_change(self) -> None:
        initial = Database(self.db_path, backup_dir=self._backup_dir())
        await initial.init()
        await initial.close()

        async def add_marker(conn) -> None:
            await conn.execute("CREATE TABLE migration_marker (value TEXT NOT NULL)")

        upgraded = Database(
            self.db_path,
            backup_dir=self._backup_dir(),
            migrations=(*DEFAULT_MIGRATIONS, Migration(CURRENT_SCHEMA_VERSION + 1, "add_marker", add_marker)),
        )
        await upgraded.init()
        backups = self._backup_files()
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        try:
            self.assertIsNone(
                backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_marker'"
                ).fetchone()
            )
        finally:
            backup.close()
        marker = await upgraded.conn.execute("SELECT name FROM sqlite_master WHERE name = 'migration_marker'")
        self.assertIsNotNone(await marker.fetchone())
        await upgraded.close()

    async def test_empty_database_creates_no_backup(self) -> None:
        db = Database(self.db_path, backup_dir=self._backup_dir())
        await db.init()
        self.assertEqual(self._backup_files(), [])
        await db.close()

    async def test_backup_passes_integrity_and_foreign_key_checks(self) -> None:
        await self._create_legacy_database(with_data=True)
        manager = SQLiteBackupManager(
            source_path=self.db_path,
            backup_dir=self._backup_dir(),
            keep=7,
        )
        db = Database(
            self.db_path,
            backup_dir=self._backup_dir(),
            backup_manager=manager,
        )
        await db.init()
        backup = self._backup_files()[0]
        manager.verify_backup(backup)
        await db.close()

    async def test_invalid_backup_blocks_migration_before_schema_change(self) -> None:
        await self._create_legacy_database(with_data=True)

        class InvalidBackupManager(SQLiteBackupManager):
            async def create_backup(self, source, *, target_version):
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                broken = self.backup_dir / "broken.sqlite3"
                broken.write_text("not sqlite", encoding="utf-8")
                return broken

        manager = InvalidBackupManager(
            source_path=self.db_path,
            backup_dir=self._backup_dir(),
            keep=7,
        )
        db = Database(self.db_path, backup_manager=manager)
        with self.assertRaises(DatabaseBackupError):
            await db.init()
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            )
        finally:
            conn.close()

    async def test_backup_creation_error_blocks_migration(self) -> None:
        await self._create_legacy_database()

        class FailingBackupManager(SQLiteBackupManager):
            async def create_backup(self, source, *, target_version):
                raise DatabaseBackupError("intentional backup failure")

        manager = FailingBackupManager(
            source_path=self.db_path,
            backup_dir=self._backup_dir(),
            keep=7,
        )
        db = Database(self.db_path, backup_manager=manager)
        with self.assertRaisesRegex(DatabaseBackupError, "intentional backup failure"):
            await db.init()
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            )
        finally:
            conn.close()

    async def test_backup_restores_pre_migration_data(self) -> None:
        await self._create_legacy_database(with_data=True)
        baseline = Database(self.db_path, backup_dir=self._backup_dir())
        await baseline.init()
        await baseline.close()
        for backup in self._backup_files():
            backup.unlink()

        async def rename_group(conn) -> None:
            await conn.execute(
                "UPDATE channels SET group_title = 'Migrated group' WHERE owner_id = 1"
            )

        upgraded = Database(
            self.db_path,
            backup_dir=self._backup_dir(),
            migrations=(*DEFAULT_MIGRATIONS, Migration(CURRENT_SCHEMA_VERSION + 1, "rename_group", rename_group)),
        )
        await upgraded.init()
        current = await upgraded.conn.execute(
            "SELECT group_title FROM channels WHERE owner_id = 1"
        )
        self.assertEqual((await current.fetchone())["group_title"], "Migrated group")
        await upgraded.close()

        backup = sqlite3.connect(self._backup_files()[0])
        try:
            self.assertEqual(
                backup.execute("SELECT group_title FROM channels WHERE owner_id = 1").fetchone()[0],
                "Legacy group",
            )
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            backup.close()

    async def test_backup_names_are_unique_and_rotation_preserves_other_files(self) -> None:
        source = await aiosqlite.connect(self.db_path)
        await apply_legacy_schema(source)
        await source.commit()
        manager = SQLiteBackupManager(
            source_path=self.db_path,
            backup_dir=self._backup_dir(),
            keep=2,
        )
        other_file = self._backup_dir() / "do-not-delete.txt"
        self._backup_dir().mkdir(parents=True, exist_ok=True)
        other_file.write_text("keep", encoding="utf-8")
        created = []
        for _ in range(3):
            backup = await manager.create_backup(source, target_version=2)
            manager.verify_backup(backup)
            created.append(backup.name)
        manager.rotate_after_success()
        await source.close()

        self.assertEqual(len(set(created)), 3)
        self.assertEqual(len(self._backup_files()), 2)
        self.assertTrue(other_file.exists())

    async def test_preflight_rejects_orphaned_legacy_foreign_key(self) -> None:
        await self._create_legacy_database()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO tenant_subscribers VALUES (?, ?, ?, ?)",
                (999, 888, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        db = Database(self.db_path)
        with self.assertRaises(DatabasePreflightError):
            await db.init()


if __name__ == "__main__":
    unittest.main()
