import sqlite3
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from database import (
    DEFAULT_MIGRATIONS,
    Database,
    DatabaseMigrationError,
    DatabasePreflightError,
    Migration,
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
        db = Database(self.db_path)
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
        self.assertEqual(await self._migration_versions(), [1])
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
        self.assertEqual(await self._migration_versions(), [1])
        await second.close()

    async def test_failed_migration_rolls_back_and_is_not_recorded(self) -> None:
        async def fail_after_write(conn) -> None:
            await conn.execute("CREATE TABLE rollback_probe (id INTEGER)")
            raise RuntimeError("intentional migration failure")

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
                Migration(2, "intentional_failure", fail_after_write),
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "intentional migration failure"):
            await db.init()

        self.assertEqual(await self._migration_versions(), [1])
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
        tenant = await db.conn.execute("SELECT * FROM tenants WHERE owner_id = 1")
        user = await db.conn.execute("SELECT * FROM users WHERE user_id = 2")
        topic = await db.conn.execute("SELECT * FROM topics WHERE owner_id = 1 AND user_id = 2")
        notices = await db.conn.execute("SELECT COUNT(*) AS count FROM notification_log")
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
        await db.set_tenant_period(1, 31)
        await db.advance_tenant_reset(
            owner_id=1,
            next_reset_at=datetime.now(timezone.utc),
        )
        notices = await db.conn.execute(
            "SELECT COUNT(*) AS count FROM notification_log"
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
                    Migration(3, "gap", noop),
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
                Migration(2, "second", noop),
                Migration(3, "third", noop),
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
                Migration(2, "second", noop),
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
                "INSERT INTO tenant_subscribers VALUES (?, ?, ?, ?)",
                (999, 888, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )

        db = Database(
            self.db_path,
            migrations=(
                *DEFAULT_MIGRATIONS,
                Migration(2, "deferred_foreign_key_violation", create_deferred_foreign_key_violation),
            ),
        )
        with self.assertRaises(DatabasePreflightError):
            await db.init()

        self.assertEqual(await self._migration_versions(), [1])
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM tenant_subscribers"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

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
