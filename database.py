import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite


MigrationApply = Callable[[aiosqlite.Connection], Awaitable[None]]


class DatabaseMigrationError(RuntimeError):
    """Migration history or database schema is incompatible with this application."""


class DatabasePreflightError(DatabaseMigrationError):
    """SQLite did not pass mandatory checks before migration."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: MigrationApply


LEGACY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        owner_id INTEGER PRIMARY KEY,
        group_id INTEGER NOT NULL UNIQUE,
        group_title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        reset_interval_days INTEGER NOT NULL DEFAULT 30,
        notice_text TEXT NOT NULL,
        timezone_name TEXT NOT NULL,
        next_reset_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT NOT NULL,
        last_name TEXT,
        username TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        blocked INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_subscribers (
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, user_id),
        FOREIGN KEY (owner_id) REFERENCES tenants(owner_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tenant_subscribers_owner ON tenant_subscribers(owner_id)",
    """
    CREATE TABLE IF NOT EXISTS active_tenant (
        user_id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        selected_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
        FOREIGN KEY (owner_id) REFERENCES tenants(owner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, user_id),
        UNIQUE (group_id, topic_id),
        FOREIGN KEY (owner_id) REFERENCES tenants(owner_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_topics_owner_created ON topics(owner_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_topics_group_topic ON topics(group_id, topic_id)",
    """
    CREATE TABLE IF NOT EXISTS notification_log (
        owner_id INTEGER NOT NULL,
        cycle_at TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, cycle_at, user_id),
        FOREIGN KEY (owner_id) REFERENCES tenants(owner_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
    """,
)

LEGACY_COLUMNS = {
    "tenants": {
        "owner_id", "group_id", "group_title", "created_at", "updated_at",
        "reset_interval_days", "notice_text", "timezone_name", "next_reset_at",
        "enabled",
    },
    "users": {
        "user_id", "first_name", "last_name", "username", "first_seen_at",
        "last_seen_at", "blocked",
    },
    "tenant_subscribers": {
        "owner_id", "user_id", "first_seen_at", "last_seen_at",
    },
    "active_tenant": {"user_id", "owner_id", "selected_at"},
    "topics": {
        "owner_id", "user_id", "group_id", "topic_id", "created_at",
        "last_activity_at",
    },
    "notification_log": {"owner_id", "cycle_at", "user_id", "sent_at"},
}
LEGACY_PRIMARY_KEYS = {
    "tenants": ("owner_id",),
    "users": ("user_id",),
    "tenant_subscribers": ("owner_id", "user_id"),
    "active_tenant": ("user_id",),
    "topics": ("owner_id", "user_id"),
    "notification_log": ("owner_id", "cycle_at", "user_id"),
}
LEGACY_FOREIGN_KEYS = {
    "tenant_subscribers": {
        ("owner_id", "tenants", "owner_id", "CASCADE"),
        ("user_id", "users", "user_id", "CASCADE"),
    },
    "active_tenant": {
        ("user_id", "users", "user_id", "CASCADE"),
        ("owner_id", "tenants", "owner_id", "CASCADE"),
    },
    "topics": {
        ("owner_id", "tenants", "owner_id", "CASCADE"),
        ("user_id", "users", "user_id", "CASCADE"),
    },
    "notification_log": {
        ("owner_id", "tenants", "owner_id", "CASCADE"),
        ("user_id", "users", "user_id", "CASCADE"),
    },
}
LEGACY_UNIQUE_CONSTRAINTS = {
    "tenants": (("group_id",),),
    "topics": (("group_id", "topic_id"),),
}
LEGACY_INDEXES = {
    "idx_tenant_subscribers_owner": ("tenant_subscribers", ("owner_id",)),
    "idx_topics_owner_created": ("topics", ("owner_id", "created_at")),
    "idx_topics_group_topic": ("topics", ("group_id", "topic_id")),
}


async def apply_legacy_schema(conn: aiosqlite.Connection) -> None:
    for statement in LEGACY_SCHEMA_STATEMENTS:
        await conn.execute(statement)


DEFAULT_MIGRATIONS = (
    Migration(1, "baseline_legacy_schema", apply_legacy_schema),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_db(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def dt_from_db(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class Database:
    def __init__(
        self,
        path: str,
        *,
        migrations: Sequence[Migration] | None = None,
    ) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._migrations = tuple(
            DEFAULT_MIGRATIONS if migrations is None else migrations
        )
        self._validate_migrations()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() must be called first")
        return self._conn

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = sqlite3.Row
            await self.conn.execute("PRAGMA foreign_keys = ON")
            await self.conn.execute("PRAGMA journal_mode = WAL")
            await self.conn.execute("PRAGMA synchronous = NORMAL")
            await self.conn.execute("PRAGMA busy_timeout = 5000")
            await self.run_preflight()
            await self._ensure_migration_table()
            await self._validate_migration_table()
            await self._apply_pending_migrations()
            await self.run_preflight()
        except Exception:
            await self.close()
            raise

    def _validate_migrations(self) -> None:
        versions = [migration.version for migration in self._migrations]
        expected = list(range(1, len(versions) + 1))
        if versions != expected:
            raise ValueError(
                "Migration registry must start at 1 and have no gaps"
            )
        if any(not migration.name.strip() for migration in self._migrations):
            raise ValueError("Migration name cannot be empty")

    async def run_preflight(self) -> None:
        """Checks database integrity and foreign keys around migrations."""
        try:
            integrity_cursor = await self.conn.execute("PRAGMA integrity_check")
            integrity = [str(row[0]) for row in await integrity_cursor.fetchall()]
            if integrity != ["ok"]:
                raise DatabasePreflightError(
                    "SQLite integrity_check failed: "
                    + "; ".join(integrity)
                )
            foreign_key_cursor = await self.conn.execute(
                "PRAGMA foreign_key_check"
            )
            foreign_key_rows = await foreign_key_cursor.fetchall()
            if foreign_key_rows:
                raise DatabasePreflightError(
                    "SQLite foreign_key_check found broken references: "
                    f"{len(foreign_key_rows)}"
                )
        except DatabasePreflightError:
            raise
        except sqlite3.Error as exc:
            raise DatabasePreflightError(
                "Unable to run SQLite preflight: " + str(exc)
            ) from exc

    async def _ensure_migration_table(self) -> None:
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        await self.conn.commit()

    async def _validate_migration_table(self) -> None:
        columns = await self._table_columns("schema_migrations")
        if not {"version", "name", "applied_at"} <= columns:
            raise DatabaseMigrationError(
                "schema_migrations has an incompatible structure"
            )
        if await self._primary_key("schema_migrations") != ("version",):
            raise DatabaseMigrationError(
                "schema_migrations must have PRIMARY KEY(version)"
            )

    async def _apply_pending_migrations(self) -> None:
        history = await self._read_migration_history()
        self._validate_migration_history(history)
        applied_versions = {version for version, _ in history}

        for migration in self._migrations:
            if migration.version in applied_versions:
                continue
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                if migration.version == 1 and not history:
                    await self._apply_or_validate_baseline(migration)
                else:
                    await migration.apply(self.conn)
                await self.run_preflight()
                await self.conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, migration.name, dt_to_db(utc_now())),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def _read_migration_history(self) -> list[tuple[int, str]]:
        cursor = await self.conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
        return [
            (int(row["version"]), str(row["name"]))
            for row in await cursor.fetchall()
        ]

    def _validate_migration_history(
        self,
        history: Sequence[tuple[int, str]],
    ) -> None:
        expected_prefix = [
            (migration.version, migration.name)
            for migration in self._migrations[:len(history)]
        ]
        if list(history) != expected_prefix:
            raise DatabaseMigrationError(
                "schema_migrations history is incompatible with the current "
                "migration registry; startup stopped"
            )

    async def _apply_or_validate_baseline(self, migration: Migration) -> None:
        existing = await self._existing_legacy_tables()
        if not existing:
            await migration.apply(self.conn)
            return
        await self._validate_legacy_schema(existing)

    async def _existing_legacy_tables(self) -> set[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        names = {str(row["name"]) for row in await cursor.fetchall()}
        return names & set(LEGACY_COLUMNS)

    async def _validate_legacy_schema(self, existing: set[str]) -> None:
        expected_tables = set(LEGACY_COLUMNS)
        if existing != expected_tables:
            missing = ", ".join(sorted(expected_tables - existing))
            raise DatabaseMigrationError(
                "Partial legacy schema detected; missing tables: "
                + (missing or "unknown")
            )

        for table, required_columns in LEGACY_COLUMNS.items():
            columns = await self._table_columns(table)
            missing_columns = required_columns - columns
            if missing_columns:
                raise DatabaseMigrationError(
                    f"Legacy table {table} is missing required columns: "
                    + ", ".join(sorted(missing_columns))
                )
            if await self._primary_key(table) != LEGACY_PRIMARY_KEYS[table]:
                raise DatabaseMigrationError(
                    f"Legacy table {table} has an incompatible PRIMARY KEY"
                )

        for table, expected_foreign_keys in LEGACY_FOREIGN_KEYS.items():
            actual_foreign_keys = await self._foreign_keys(table)
            if not expected_foreign_keys <= actual_foreign_keys:
                raise DatabaseMigrationError(
                    f"Legacy table {table} is missing required FOREIGN KEY constraints"
                )

        for table, constraints in LEGACY_UNIQUE_CONSTRAINTS.items():
            for columns in constraints:
                if not await self._has_unique_index(table, columns):
                    raise DatabaseMigrationError(
                        f"Legacy table {table} is missing UNIQUE{columns}"
                    )

        for index_name, (table, columns) in LEGACY_INDEXES.items():
            if await self._index_columns(index_name) != columns:
                raise DatabaseMigrationError(
                    f"Legacy schema is missing critical index {index_name}"
                )

    async def _table_columns(self, table: str) -> set[str]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        return {str(row["name"]) for row in await cursor.fetchall()}

    async def _primary_key(self, table: str) -> tuple[str, ...]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return tuple(
            str(row["name"])
            for row in sorted(rows, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        )

    async def _foreign_keys(self, table: str) -> set[tuple[str, str, str, str]]:
        cursor = await self.conn.execute(f"PRAGMA foreign_key_list({table})")
        return {
            (
                str(row["from"]), str(row["table"]), str(row["to"]),
                str(row["on_delete"]).upper(),
            )
            for row in await cursor.fetchall()
        }

    async def _has_unique_index(
        self,
        table: str,
        columns: tuple[str, ...],
    ) -> bool:
        cursor = await self.conn.execute(f"PRAGMA index_list({table})")
        for index in await cursor.fetchall():
            if int(index["unique"]) and (
                await self._index_columns(str(index["name"])) == columns
            ):
                return True
        return False

    async def _index_columns(self, index_name: str) -> tuple[str, ...] | None:
        cursor = await self.conn.execute(f"PRAGMA index_info({index_name})")
        rows = await cursor.fetchall()
        if not rows:
            return None
        return tuple(
            str(row["name"])
            for row in sorted(rows, key=lambda row: int(row["seqno"]))
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------

    async def register_tenant(
        self,
        *,
        owner_id: int,
        group_id: int,
        group_title: str,
        default_reset_days: int,
        default_notice_text: str,
        default_timezone: str,
    ) -> tuple[str, sqlite3.Row | None]:
        """
        Один owner_id может владеть одной супергруппой.
        Одна супергруппа может принадлежать только одному owner_id.

        Возвращает:
        - "created"
        - "existing"
        - "owner_has_other_group"
        - "group_has_other_owner"
        """
        now = utc_now()

        async with self._write_lock:
            by_group_cursor = await self.conn.execute(
                "SELECT * FROM tenants WHERE group_id = ?",
                (group_id,),
            )
            by_group = await by_group_cursor.fetchone()

            if by_group is not None and int(by_group["owner_id"]) != owner_id:
                return "group_has_other_owner", by_group

            by_owner_cursor = await self.conn.execute(
                "SELECT * FROM tenants WHERE owner_id = ?",
                (owner_id,),
            )
            by_owner = await by_owner_cursor.fetchone()

            if by_owner is not None and int(by_owner["group_id"]) != group_id:
                return "owner_has_other_group", by_owner

            if by_owner is not None:
                await self.conn.execute(
                    """
                    UPDATE tenants
                    SET group_title = ?, updated_at = ?, enabled = 1
                    WHERE owner_id = ?
                    """,
                    (group_title, dt_to_db(now), owner_id),
                )
                await self.conn.commit()

                cursor = await self.conn.execute(
                    "SELECT * FROM tenants WHERE owner_id = ?",
                    (owner_id,),
                )
                return "existing", await cursor.fetchone()

            next_reset = now + timedelta(days=default_reset_days)

            await self.conn.execute(
                """
                INSERT INTO tenants (
                    owner_id,
                    group_id,
                    group_title,
                    created_at,
                    updated_at,
                    reset_interval_days,
                    notice_text,
                    timezone_name,
                    next_reset_at,
                    enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    owner_id,
                    group_id,
                    group_title,
                    dt_to_db(now),
                    dt_to_db(now),
                    default_reset_days,
                    default_notice_text,
                    default_timezone,
                    dt_to_db(next_reset),
                ),
            )
            await self.conn.commit()

            cursor = await self.conn.execute(
                "SELECT * FROM tenants WHERE owner_id = ?",
                (owner_id,),
            )
            return "created", await cursor.fetchone()

    async def get_tenant_by_owner(
        self, owner_id: int
    ) -> sqlite3.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM tenants WHERE owner_id = ? AND enabled = 1",
            (owner_id,),
        )
        return await cursor.fetchone()

    async def get_tenant_by_group(
        self, group_id: int
    ) -> sqlite3.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM tenants WHERE group_id = ? AND enabled = 1",
            (group_id,),
        )
        return await cursor.fetchone()

    async def list_enabled_tenants(self) -> list[sqlite3.Row]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM tenants
            WHERE enabled = 1
            ORDER BY owner_id
            """
        )
        return await cursor.fetchall()

    async def set_tenant_period(
        self,
        owner_id: int,
        days: int,
    ) -> datetime:
        if days < 2:
            raise ValueError("Период должен быть не меньше 2 дней")

        next_reset = utc_now() + timedelta(days=days)

        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE tenants
                SET reset_interval_days = ?,
                    next_reset_at = ?,
                    updated_at = ?
                WHERE owner_id = ?
                """,
                (
                    days,
                    dt_to_db(next_reset),
                    dt_to_db(utc_now()),
                    owner_id,
                ),
            )
            await self.conn.commit()

        return next_reset

    async def set_tenant_notice(
        self,
        owner_id: int,
        text: str,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE tenants
                SET notice_text = ?, updated_at = ?
                WHERE owner_id = ?
                """,
                (text, dt_to_db(utc_now()), owner_id),
            )
            await self.conn.commit()

    async def set_tenant_timezone(
        self,
        owner_id: int,
        timezone_name: str,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE tenants
                SET timezone_name = ?, updated_at = ?
                WHERE owner_id = ?
                """,
                (timezone_name, dt_to_db(utc_now()), owner_id),
            )
            await self.conn.commit()

    async def advance_tenant_reset(
        self,
        *,
        owner_id: int,
        next_reset_at: datetime,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE tenants
                SET next_reset_at = ?, updated_at = ?
                WHERE owner_id = ?
                """,
                (
                    dt_to_db(next_reset_at),
                    dt_to_db(utc_now()),
                    owner_id,
                ),
            )
            await self.conn.commit()

    # ------------------------------------------------------------------
    # Users and tenant memberships
    # ------------------------------------------------------------------

    async def upsert_user(
        self,
        *,
        user_id: int,
        first_name: str,
        last_name: str | None,
        username: str | None,
    ) -> None:
        now = dt_to_db(utc_now())

        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    first_name,
                    last_name,
                    username,
                    first_seen_at,
                    last_seen_at,
                    blocked
                )
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username = excluded.username,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user_id,
                    first_name,
                    last_name,
                    username,
                    now,
                    now,
                ),
            )
            await self.conn.commit()

    async def set_user_blocked(
        self,
        user_id: int,
        blocked: bool,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE users SET blocked = ? WHERE user_id = ?",
                (1 if blocked else 0, user_id),
            )
            await self.conn.commit()

    async def attach_subscriber(
        self,
        *,
        owner_id: int,
        user_id: int,
    ) -> None:
        now = dt_to_db(utc_now())

        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO tenant_subscribers (
                    owner_id,
                    user_id,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id, user_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (owner_id, user_id, now, now),
            )

            await self.conn.execute(
                """
                INSERT INTO active_tenant (
                    user_id,
                    owner_id,
                    selected_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    selected_at = excluded.selected_at
                """,
                (user_id, owner_id, now),
            )

            await self.conn.commit()

    async def touch_subscriber(
        self,
        *,
        owner_id: int,
        user_id: int,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE tenant_subscribers
                SET last_seen_at = ?
                WHERE owner_id = ? AND user_id = ?
                """,
                (dt_to_db(utc_now()), owner_id, user_id),
            )
            await self.conn.commit()

    async def get_active_tenant_for_user(
        self,
        user_id: int,
    ) -> sqlite3.Row | None:
        cursor = await self.conn.execute(
            """
            SELECT t.*
            FROM active_tenant a
            JOIN tenants t ON t.owner_id = a.owner_id
            WHERE a.user_id = ?
              AND t.enabled = 1
            """,
            (user_id,),
        )
        return await cursor.fetchone()

    async def count_tenant_subscribers(self, owner_id: int) -> int:
        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM tenant_subscribers
            WHERE owner_id = ?
            """,
            (owner_id,),
        )
        row = await cursor.fetchone()
        return int(row["c"])

    async def get_unnotified_subscribers(
        self,
        *,
        owner_id: int,
        cycle_at: str,
    ) -> list[int]:
        cursor = await self.conn.execute(
            """
            SELECT s.user_id
            FROM tenant_subscribers s
            LEFT JOIN notification_log n
                ON n.owner_id = s.owner_id
               AND n.user_id = s.user_id
               AND n.cycle_at = ?
            WHERE s.owner_id = ?
              AND n.user_id IS NULL
            ORDER BY s.user_id
            """,
            (cycle_at, owner_id),
        )
        rows = await cursor.fetchall()
        return [int(row["user_id"]) for row in rows]

    async def mark_notification_sent(
        self,
        *,
        owner_id: int,
        cycle_at: str,
        user_id: int,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO notification_log (
                    owner_id,
                    cycle_at,
                    user_id,
                    sent_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    owner_id,
                    cycle_at,
                    user_id,
                    dt_to_db(utc_now()),
                ),
            )
            await self.conn.commit()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    async def get_topic_for_user(
        self,
        *,
        owner_id: int,
        user_id: int,
    ) -> sqlite3.Row | None:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM topics
            WHERE owner_id = ? AND user_id = ?
            """,
            (owner_id, user_id),
        )
        return await cursor.fetchone()

    async def get_topic_by_group_thread(
        self,
        *,
        group_id: int,
        topic_id: int,
    ) -> sqlite3.Row | None:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM topics
            WHERE group_id = ? AND topic_id = ?
            """,
            (group_id, topic_id),
        )
        return await cursor.fetchone()

    async def create_topic_mapping(
        self,
        *,
        owner_id: int,
        user_id: int,
        group_id: int,
        topic_id: int,
    ) -> None:
        now = dt_to_db(utc_now())

        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO topics (
                    owner_id,
                    user_id,
                    group_id,
                    topic_id,
                    created_at,
                    last_activity_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, user_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    topic_id = excluded.topic_id,
                    created_at = excluded.created_at,
                    last_activity_at = excluded.last_activity_at
                """,
                (
                    owner_id,
                    user_id,
                    group_id,
                    topic_id,
                    now,
                    now,
                ),
            )
            await self.conn.commit()

    async def touch_topic(
        self,
        *,
        owner_id: int,
        user_id: int,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE topics
                SET last_activity_at = ?
                WHERE owner_id = ? AND user_id = ?
                """,
                (dt_to_db(utc_now()), owner_id, user_id),
            )
            await self.conn.commit()

    async def delete_topic_mapping(
        self,
        *,
        owner_id: int,
        user_id: int,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                DELETE FROM topics
                WHERE owner_id = ? AND user_id = ?
                """,
                (owner_id, user_id),
            )
            await self.conn.commit()

    async def topics_created_before(
        self,
        *,
        owner_id: int,
        cutoff: datetime,
    ) -> list[sqlite3.Row]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM topics
            WHERE owner_id = ?
              AND created_at < ?
            ORDER BY created_at ASC
            """,
            (owner_id, dt_to_db(cutoff)),
        )
        return await cursor.fetchall()

    async def count_tenant_topics(self, owner_id: int) -> int:
        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM topics
            WHERE owner_id = ?
            """,
            (owner_id,),
        )
        row = await cursor.fetchone()
        return int(row["c"])
