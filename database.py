import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_db(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def dt_from_db(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() должен быть вызван первым")
        return self._conn

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = sqlite3.Row

        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA synchronous = NORMAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")

        await self.conn.executescript(
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
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT,
                username TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tenant_subscribers (
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, user_id),
                FOREIGN KEY (owner_id)
                    REFERENCES tenants(owner_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tenant_subscribers_owner
                ON tenant_subscribers(owner_id);

            CREATE TABLE IF NOT EXISTS active_tenant (
                user_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                selected_at TEXT NOT NULL,
                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (owner_id)
                    REFERENCES tenants(owner_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS topics (
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, user_id),
                UNIQUE (group_id, topic_id),
                FOREIGN KEY (owner_id)
                    REFERENCES tenants(owner_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_topics_owner_created
                ON topics(owner_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_topics_group_topic
                ON topics(group_id, topic_id);

            CREATE TABLE IF NOT EXISTS notification_log (
                owner_id INTEGER NOT NULL,
                cycle_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, cycle_at, user_id),
                FOREIGN KEY (owner_id)
                    REFERENCES tenants(owner_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );
            """
        )
        await self.conn.commit()

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
            await self.conn.execute(
                "DELETE FROM notification_log WHERE owner_id = ?",
                (owner_id,),
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
            await self.conn.execute(
                "DELETE FROM notification_log WHERE owner_id = ?",
                (owner_id,),
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
