import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from statistics import median

import aiosqlite


logger = logging.getLogger(__name__)


MigrationApply = Callable[[aiosqlite.Connection], Awaitable[None]]


class DatabaseMigrationError(RuntimeError):
    """Migration history or database schema is incompatible with this application."""


class DatabasePreflightError(DatabaseMigrationError):
    """SQLite did not pass mandatory checks before migration."""


class DatabaseBackupError(DatabaseMigrationError):
    """A required pre-migration SQLite backup could not be verified."""


class DraftConflictError(RuntimeError):
    """A channel customization draft is based on a stale live revision."""


class DraftNotEmptyError(RuntimeError):
    """An operation requires an empty channel customization draft."""


class SQLiteBackupManager:
    """Creates SQLite-aware local restore points for this application only."""

    def __init__(
        self,
        *,
        source_path: str,
        backup_dir: str | Path,
        keep: int,
    ) -> None:
        if keep < 1:
            raise ValueError("DATABASE_BACKUP_KEEP must be at least 1")
        self.source_path = Path(source_path)
        self.backup_dir = Path(backup_dir)
        self.keep = keep
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", self.source_path.stem)
        source_hash = hashlib.sha256(
            str(self.source_path.absolute()).encode("utf-8")
        ).hexdigest()[:12]
        self._filename_prefix = f"{safe_stem or 'database'}_{source_hash}"

    async def create_backup(
        self,
        source: aiosqlite.Connection,
        *,
        target_version: int,
    ) -> Path:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            nonce = uuid.uuid4().hex[:12]
            filename = (
                f"{self._filename_prefix}_pre_migration_to_v{target_version}_"
                f"{timestamp}_{nonce}.sqlite3"
            )
            destination = self.backup_dir / filename
            temporary = destination.with_suffix(".sqlite3.tmp")
            target = sqlite3.connect(temporary)
            try:
                await source.backup(target)
            finally:
                target.close()
            temporary.replace(destination)
            return destination
        except Exception as exc:
            try:
                if 'temporary' in locals() and temporary.exists():
                    temporary.unlink()
            except OSError:
                logger.warning("Could not remove incomplete SQLite backup", exc_info=True)
            raise DatabaseBackupError(
                "Unable to create pre-migration SQLite backup: " + str(exc)
            ) from exc

    def verify_backup(self, backup_path: Path) -> None:
        if not backup_path.is_file():
            raise DatabaseBackupError("SQLite backup file was not created")
        try:
            connection = sqlite3.connect(f"{backup_path.absolute().as_uri()}?mode=ro", uri=True)
            try:
                integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
                if integrity != ["ok"]:
                    raise DatabaseBackupError(
                        "SQLite backup integrity_check failed: " + "; ".join(integrity)
                    )
                foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
                if foreign_keys:
                    raise DatabaseBackupError(
                        "SQLite backup foreign_key_check found broken references: "
                        f"{len(foreign_keys)}"
                    )
            finally:
                connection.close()
        except DatabaseBackupError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseBackupError(
                "Unable to verify SQLite backup: " + str(exc)
            ) from exc

    def rotate_after_success(self) -> None:
        try:
            backups = sorted(
                self.backup_dir.glob(
                    f"{self._filename_prefix}_pre_migration_to_v*.sqlite3"
                ),
                key=lambda item: (item.stat().st_mtime_ns, item.name),
                reverse=True,
            )
        except OSError:
            logger.warning("Could not enumerate local SQLite backups", exc_info=True)
            return

        for stale_backup in backups[self.keep:]:
            try:
                stale_backup.unlink()
            except OSError:
                logger.warning(
                    "Could not remove expired local SQLite backup %s",
                    stale_backup,
                    exc_info=True,
                )


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


CHANNEL_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE channels (
        channel_id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL UNIQUE,
        group_title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        reset_interval_days INTEGER NOT NULL DEFAULT 30,
        notice_text TEXT NOT NULL,
        timezone_name TEXT NOT NULL,
        next_reset_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        auto_cleanup_enabled INTEGER NOT NULL DEFAULT 1,
        anonymous_prefix TEXT NOT NULL DEFAULT '\u0410\u043d\u043e\u043d'
    )
    """,
    """CREATE TABLE legacy_owner_channels (
        owner_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL UNIQUE,
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE channel_anonymous_counters (
        channel_id INTEGER PRIMARY KEY,
        next_number INTEGER NOT NULL DEFAULT 1 CHECK (next_number >= 1),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE channel_subscribers (
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (channel_id, user_id),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX idx_channel_subscribers_channel ON channel_subscribers(channel_id)",
    """CREATE TABLE active_channel (
        user_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        selected_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE channel_topics (
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL,
        PRIMARY KEY (channel_id, user_id),
        UNIQUE (group_id, topic_id),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX idx_channel_topics_channel_created ON channel_topics(channel_id, created_at)",
    "CREATE INDEX idx_channel_topics_group_topic ON channel_topics(group_id, topic_id)",
    """CREATE TABLE channel_notification_log (
        channel_id INTEGER NOT NULL,
        cycle_at TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (channel_id, cycle_at, user_id),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""",
)

async def apply_channel_model(conn: aiosqlite.Connection) -> None:
    # Every join is keyed by the immutable legacy owner_id; channel ids are never
    # inferred from row order.
    await conn.execute("ALTER TABLE tenants RENAME TO legacy_tenants")
    await conn.execute("ALTER TABLE tenant_subscribers RENAME TO legacy_tenant_subscribers")
    await conn.execute("ALTER TABLE active_tenant RENAME TO legacy_active_tenant")
    await conn.execute("ALTER TABLE topics RENAME TO legacy_topics")
    await conn.execute("ALTER TABLE notification_log RENAME TO legacy_notification_log")
    for statement in CHANNEL_SCHEMA_STATEMENTS:
        await conn.execute(statement)
    await conn.execute("""INSERT INTO channels (owner_id, group_id, group_title, created_at, updated_at, reset_interval_days, notice_text, timezone_name, next_reset_at, enabled, auto_cleanup_enabled, anonymous_prefix)
        SELECT owner_id, group_id, group_title, created_at, updated_at, reset_interval_days, notice_text, timezone_name, next_reset_at, enabled, 1, '\u0410\u043d\u043e\u043d'
        FROM legacy_tenants ORDER BY owner_id""")
    await conn.execute("""INSERT INTO legacy_owner_channels (owner_id, channel_id)
        SELECT l.owner_id, c.channel_id FROM legacy_tenants l JOIN channels c ON c.group_id=l.group_id ORDER BY l.owner_id""")
    await conn.execute("INSERT INTO channel_anonymous_counters (channel_id, next_number) SELECT channel_id, 1 FROM channels")
    await conn.execute("""INSERT INTO channel_subscribers SELECT m.channel_id, s.user_id, s.first_seen_at, s.last_seen_at FROM legacy_tenant_subscribers s JOIN legacy_owner_channels m ON m.owner_id=s.owner_id""")
    await conn.execute("""INSERT INTO active_channel SELECT a.user_id, m.channel_id, a.selected_at FROM legacy_active_tenant a JOIN legacy_owner_channels m ON m.owner_id=a.owner_id""")
    await conn.execute("""INSERT INTO channel_topics SELECT m.channel_id, t.user_id, t.group_id, t.topic_id, t.created_at, t.last_activity_at FROM legacy_topics t JOIN legacy_owner_channels m ON m.owner_id=t.owner_id""")
    await conn.execute("""INSERT INTO channel_notification_log SELECT m.channel_id, n.cycle_at, n.user_id, n.sent_at FROM legacy_notification_log n JOIN legacy_owner_channels m ON m.owner_id=n.owner_id""")
    for old, new in (("legacy_tenant_subscribers", "channel_subscribers"), ("legacy_active_tenant", "active_channel"), ("legacy_topics", "channel_topics"), ("legacy_notification_log", "channel_notification_log")):
        a = (await (await conn.execute(f"SELECT COUNT(*) FROM {old}")).fetchone())[0]
        b = (await (await conn.execute(f"SELECT COUNT(*) FROM {new}")).fetchone())[0]
        if a != b: raise DatabaseMigrationError(f"Channel migration lost rows from {old}")
    await conn.execute("DROP TABLE legacy_tenant_subscribers")
    await conn.execute("DROP TABLE legacy_active_tenant")
    await conn.execute("DROP TABLE legacy_topics")
    await conn.execute("DROP TABLE legacy_notification_log")
    await conn.execute("DROP TABLE legacy_tenants")


async def apply_privacy_model(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channel_topics RENAME TO legacy_channel_topics")
    await conn.execute("DROP INDEX IF EXISTS idx_channel_topics_channel_created")
    await conn.execute("DROP INDEX IF EXISTS idx_channel_topics_group_topic")
    await conn.execute("""CREATE TABLE channel_topics (
        channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        privacy_mode TEXT NOT NULL CHECK (privacy_mode IN ('identified','anonymous')),
        group_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
        PRIMARY KEY (channel_id,user_id,privacy_mode), UNIQUE(group_id,topic_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_channel_topics_channel_created ON channel_topics(channel_id, created_at)")
    await conn.execute("CREATE INDEX idx_channel_topics_group_topic ON channel_topics(group_id, topic_id)")
    await conn.execute("""INSERT INTO channel_topics(channel_id,user_id,privacy_mode,group_id,topic_id,created_at,last_activity_at)
        SELECT channel_id,user_id,'identified',group_id,topic_id,created_at,last_activity_at FROM legacy_channel_topics""")
    await conn.execute("DROP TABLE legacy_channel_topics")
    await conn.execute("""CREATE TABLE channel_subscriber_privacy (
        channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        privacy_mode TEXT NOT NULL CHECK (privacy_mode IN ('identified','anonymous')),
        updated_at TEXT NOT NULL, PRIMARY KEY(channel_id,user_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("""CREATE TABLE anonymous_tags (
        channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, cycle_key TEXT NOT NULL,
        number INTEGER NOT NULL, tag TEXT NOT NULL, assigned_at TEXT NOT NULL,
        PRIMARY KEY(channel_id,user_id,cycle_key), UNIQUE(channel_id,cycle_key,number),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")



async def apply_message_event_log(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE message_events (
        event_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        privacy_mode TEXT NOT NULL CHECK (privacy_mode IN ('identified','anonymous')),
        direction TEXT NOT NULL CHECK (direction IN ('subscriber_to_admin','admin_to_subscriber')),
        message_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        admin_id INTEGER,
        media_group_id TEXT,
        UNIQUE(source_chat_id, source_message_id, direction),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_message_events_channel_time ON message_events(channel_id, occurred_at)")
    await conn.execute("CREATE INDEX idx_message_events_channel_user_time ON message_events(channel_id, user_id, occurred_at)")



async def apply_topic_statuses(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channel_topics ADD COLUMN status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','in_progress','answered','closed'))")
    await conn.execute("CREATE INDEX idx_channel_topics_channel_status ON channel_topics(channel_id, status)")


async def apply_topic_cleanup_protection(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channel_topics ADD COLUMN is_important INTEGER NOT NULL DEFAULT 0 CHECK (is_important IN (0, 1))")
    await conn.execute("ALTER TABLE channel_topics ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1))")
    await conn.execute("CREATE INDEX idx_channel_topics_cleanup_eligibility ON channel_topics(channel_id, created_at, status, is_important, is_pinned)")


async def apply_channel_cleanup_policy(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channels ADD COLUMN cleanup_basis TEXT NOT NULL DEFAULT 'created_at' CHECK (cleanup_basis IN ('created_at', 'last_activity_at'))")
    await conn.execute("ALTER TABLE channels ADD COLUMN cleanup_status_scope TEXT NOT NULL DEFAULT 'all' CHECK (cleanup_status_scope IN ('all', 'answered_closed'))")
    await conn.execute("ALTER TABLE channels ADD COLUMN cleanup_action TEXT NOT NULL DEFAULT 'delete' CHECK (cleanup_action IN ('delete', 'close', 'close_then_delete'))")
    await conn.execute("ALTER TABLE channels ADD COLUMN cleanup_final_delete_days INTEGER NOT NULL DEFAULT 7 CHECK (cleanup_final_delete_days >= 1)")
    await conn.execute("ALTER TABLE channel_topics ADD COLUMN auto_closed_at TEXT")
    await conn.execute("CREATE INDEX idx_channel_topics_auto_closed ON channel_topics(channel_id, auto_closed_at)")


async def apply_active_admin_channel(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE active_admin_channel (
        owner_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        selected_at TEXT NOT NULL,
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_active_admin_channel_channel ON active_admin_channel(channel_id)")


async def apply_channel_topic_templates(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channels ADD COLUMN identified_topic_template TEXT NOT NULL DEFAULT '{name} — {username}'")
    await conn.execute("ALTER TABLE channels ADD COLUMN anonymous_topic_template TEXT NOT NULL DEFAULT '{anonymous_tag}'")


async def apply_subscriber_moderation_state(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE channel_subscriber_moderation (
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        rate_limit_seconds INTEGER,
        muted_until TEXT,
        blocked_until TEXT,
        permanently_blocked INTEGER NOT NULL DEFAULT 0 CHECK (permanently_blocked IN (0, 1)),
        marked_spam INTEGER NOT NULL DEFAULT 0 CHECK (marked_spam IN (0, 1)),
        internal_note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (channel_id, user_id),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")


async def apply_moderation_log(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE moderation_log (
        log_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        expires_at TEXT,
        details TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_moderation_log_channel_user_time ON moderation_log(channel_id,user_id,created_at)")


SANCTION_REASON_CHOICES = ("spam", "flood", "insult", "rules", "advertising", "suspicious_activity", "other")
SANCTION_REASON_LABELS = {"spam": "\u0421\u043f\u0430\u043c", "flood": "\u0424\u043b\u0443\u0434", "insult": "\u041e\u0441\u043a\u043e\u0440\u0431\u043b\u0435\u043d\u0438\u044f", "rules": "\u041d\u0430\u0440\u0443\u0448\u0435\u043d\u0438\u0435 \u043f\u0440\u0430\u0432\u0438\u043b", "advertising": "\u0420\u0435\u043a\u043b\u0430\u043c\u0430", "suspicious_activity": "\u041f\u043e\u0434\u043e\u0437\u0440\u0438\u0442\u0435\u043b\u044c\u043d\u0430\u044f \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c"}

async def apply_sanction_reasons(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE channel_subscriber_moderation ADD COLUMN sanction_reason TEXT")
    await conn.execute("ALTER TABLE channel_subscriber_moderation ADD COLUMN show_reason_to_subscriber INTEGER NOT NULL DEFAULT 0 CHECK (show_reason_to_subscriber IN (0, 1))")
    await conn.execute("ALTER TABLE moderation_log ADD COLUMN show_reason_to_subscriber INTEGER NOT NULL DEFAULT 0 CHECK (show_reason_to_subscriber IN (0, 1))")


SANCTION_ACTIONS = ("rate_limit", "mute", "temporary_block", "permanent_block", "warning")


async def apply_subscriber_sanctions(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE subscriber_sanctions (
        sanction_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('rate_limit','mute','temporary_block','permanent_block','warning')),
        rate_limit_seconds INTEGER,
        expires_at TEXT,
        reason TEXT NOT NULL,
        show_reason_to_subscriber INTEGER NOT NULL CHECK(show_reason_to_subscriber IN (0, 1)),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by INTEGER,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_subscriber_sanctions_active ON subscriber_sanctions(channel_id,user_id,active,action,expires_at)")
    # Preserve currently stored legacy moderation state as independent sanctions.
    rows = await (await conn.execute("SELECT * FROM channel_subscriber_moderation")).fetchall()
    for row in rows:
        reason = str(row["sanction_reason"] or "\\u041e\\u0433\\u0440\\u0430\\u043d\\u0438\\u0447\\u0435\\u043d\\u0438\\u0435")
        visible = int(row["show_reason_to_subscriber"] or 0)
        created = str(row["updated_at"])
        if row["rate_limit_seconds"]:
            expires = dt_to_db(dt_from_db(created) + timedelta(seconds=int(row["rate_limit_seconds"])))
            await conn.execute("INSERT INTO subscriber_sanctions(channel_id,user_id,action,rate_limit_seconds,expires_at,reason,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?,?)", (row["channel_id"],row["user_id"],"rate_limit",row["rate_limit_seconds"],expires,reason,visible,created))
        if row["muted_until"]:
            await conn.execute("INSERT INTO subscriber_sanctions(channel_id,user_id,action,expires_at,reason,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)", (row["channel_id"],row["user_id"],"mute",row["muted_until"],reason,visible,created))
        if row["blocked_until"]:
            await conn.execute("INSERT INTO subscriber_sanctions(channel_id,user_id,action,expires_at,reason,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)", (row["channel_id"],row["user_id"],"temporary_block",row["blocked_until"],reason,visible,created))
        if row["permanently_blocked"]:
            await conn.execute("INSERT INTO subscriber_sanctions(channel_id,user_id,action,reason,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?)", (row["channel_id"],row["user_id"],"permanent_block",reason,visible,created))


async def apply_subscriber_metadata(conn: aiosqlite.Connection) -> None:
    """Store staff-only notes and tags independently for each channel subscriber."""
    await conn.execute("""CREATE TABLE subscriber_notes (
        note_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        admin_id INTEGER NOT NULL,
        note_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_subscriber_notes_channel_user_time ON subscriber_notes(channel_id,user_id,created_at,note_id)")
    await conn.execute("""CREATE TABLE subscriber_tags (
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        tag_key TEXT NOT NULL,
        added_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(channel_id,user_id,tag_key),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_subscriber_tags_channel_tag ON subscriber_tags(channel_id,tag)")


async def apply_subscriber_metadata_management(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE subscriber_notes ADD COLUMN updated_at TEXT")
    await conn.execute("ALTER TABLE subscriber_notes ADD COLUMN updated_by INTEGER")
    await conn.execute("ALTER TABLE subscriber_notes ADD COLUMN deleted_at TEXT")
    await conn.execute("ALTER TABLE subscriber_notes ADD COLUMN deleted_by INTEGER")
    await conn.execute("CREATE INDEX idx_subscriber_notes_active ON subscriber_notes(channel_id,user_id,deleted_at,note_id)")
    # A stable integer identifier keeps callback data small and never exposes tag text.
    await conn.execute("""CREATE TABLE subscriber_tags_new (
        tag_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        tag_key TEXT NOT NULL,
        added_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(channel_id,user_id,tag_key),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )""")
    # v14 did not enforce case-insensitive uniqueness at SQLite level. Keep one
    # deterministic representative of any logically identical legacy tag.
    await conn.execute("INSERT INTO subscriber_tags_new(channel_id,user_id,tag,tag_key,added_by,created_at) SELECT channel_id,user_id,MIN(tag),tag_key,MIN(added_by),MIN(created_at) FROM subscriber_tags GROUP BY channel_id,user_id,tag_key")
    await conn.execute("DROP TABLE subscriber_tags")
    await conn.execute("ALTER TABLE subscriber_tags_new RENAME TO subscriber_tags")
    await conn.execute("CREATE INDEX idx_subscriber_tags_channel_tag ON subscriber_tags(channel_id,tag)")


async def apply_subscriber_statistics_events(conn: aiosqlite.Connection) -> None:
    await conn.execute("ALTER TABLE message_events ADD COLUMN conversation_id INTEGER")
    await conn.execute("CREATE INDEX idx_message_events_subscriber_conversation ON message_events(channel_id,user_id,conversation_id,occurred_at)")


async def apply_channel_template_overrides(conn: aiosqlite.Connection) -> None:
    await conn.execute("""CREATE TABLE channel_template_overrides (
        channel_id INTEGER NOT NULL,
        template_key TEXT NOT NULL,
        custom_text TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        PRIMARY KEY(channel_id, template_key),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")



CUSTOM_ITEM_TYPE_TEMPLATE_TEXT = "template_text"
CUSTOM_ITEM_TYPE_LEGACY_TEMPLATE_OVERRIDE = "legacy_template_override"
CUSTOM_ITEM_TYPE_START_CARD_MEDIA = "start_card_media"


def _custom_text_payload(*, text: str, scope: str) -> str:
    """Canonical JSON payload used by the customization foundation tables."""
    return json.dumps(
        {"scope": scope, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def apply_custom_pack_foundation(conn: aiosqlite.Connection) -> None:
    """Create immutable standard/channel customization snapshots.

    Version 23 deliberately does not switch rendering away from
    ``channel_template_overrides`` yet.  It seeds a complete immutable snapshot
    for every existing channel and a global Standard Custom Pack so later
    migrations can move editing to drafts/revisions without changing the live
    Telegram behaviour in this release step.
    """
    await conn.execute("""CREATE TABLE bot_standard_custom_revisions (
        revision_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        created_by INTEGER,
        source TEXT NOT NULL,
        summary TEXT
    )""")
    await conn.execute("""CREATE TABLE bot_standard_custom_items (
        revision_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        item_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY(revision_id, item_key),
        FOREIGN KEY(revision_id) REFERENCES bot_standard_custom_revisions(revision_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_standard_custom_items_key ON bot_standard_custom_items(item_key, revision_id)")
    await conn.execute("""CREATE TABLE bot_standard_custom_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        active_revision_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        FOREIGN KEY(active_revision_id) REFERENCES bot_standard_custom_revisions(revision_id)
    )""")

    await conn.execute("""CREATE TABLE channel_custom_revisions (
        revision_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        source_standard_revision_id INTEGER,
        created_at TEXT NOT NULL,
        created_by INTEGER,
        summary TEXT,
        UNIQUE(revision_id, channel_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(source_standard_revision_id) REFERENCES bot_standard_custom_revisions(revision_id)
    )""")
    await conn.execute("CREATE INDEX idx_channel_custom_revisions_channel_time ON channel_custom_revisions(channel_id, created_at, revision_id)")
    await conn.execute("""CREATE TABLE channel_custom_items (
        revision_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        item_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY(revision_id, item_key),
        FOREIGN KEY(revision_id) REFERENCES channel_custom_revisions(revision_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_channel_custom_items_key ON channel_custom_items(item_key, revision_id)")
    await conn.execute("""CREATE TABLE channel_custom_state (
        channel_id INTEGER PRIMARY KEY,
        active_revision_id INTEGER NOT NULL,
        initial_revision_id INTEGER NOT NULL,
        source_standard_revision_id INTEGER,
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(active_revision_id, channel_id) REFERENCES channel_custom_revisions(revision_id, channel_id),
        FOREIGN KEY(initial_revision_id, channel_id) REFERENCES channel_custom_revisions(revision_id, channel_id),
        FOREIGN KEY(source_standard_revision_id) REFERENCES bot_standard_custom_revisions(revision_id)
    )""")

    await conn.execute("""CREATE TABLE customization_audit_log (
        event_id INTEGER PRIMARY KEY,
        actor_user_id INTEGER,
        scope_type TEXT NOT NULL CHECK(scope_type IN ('global_standard','channel_custom','global_profile')),
        scope_id INTEGER NOT NULL,
        channel_id INTEGER,
        action TEXT NOT NULL,
        target_key TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        CHECK(
            (scope_type = 'channel_custom' AND channel_id IS NOT NULL AND scope_id = channel_id)
            OR (scope_type IN ('global_standard','global_profile') AND channel_id IS NULL AND scope_id = 1)
        )
    )""")
    await conn.execute("CREATE INDEX idx_customization_audit_channel_time ON customization_audit_log(channel_id, created_at, event_id)")
    await conn.execute("CREATE INDEX idx_customization_audit_scope_time ON customization_audit_log(scope_type, scope_id, created_at, event_id)")

    # Import here to keep the low-level database module independent during
    # module import. templates.py itself does not import database.py.
    from templates import TEMPLATE_REGISTRY

    now = dt_to_db(utc_now())
    standard_cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "migration_seed", "Initial Standard Custom Pack from application template defaults"),
    )
    standard_revision_id = int(standard_cursor.lastrowid)
    standard_items = []
    for key, spec in sorted(TEMPLATE_REGISTRY.items()):
        standard_items.append((
            standard_revision_id,
            f"template:{key}",
            CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
            _custom_text_payload(text=spec.default, scope=spec.scope),
        ))
    if standard_items:
        await conn.executemany(
            "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
            standard_items,
        )
    await conn.execute(
        "INSERT INTO bot_standard_custom_state(singleton_id,active_revision_id,updated_at,updated_by) VALUES(1,?,?,NULL)",
        (standard_revision_id, now),
    )
    await conn.execute(
        "INSERT INTO customization_audit_log(actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at) VALUES(NULL,'global_standard',1,NULL,'migration_seed',NULL,?,?)",
        (json.dumps({"revision_id": standard_revision_id, "items": len(standard_items)}, sort_keys=True), now),
    )

    channels = await (await conn.execute("SELECT channel_id FROM channels ORDER BY channel_id")).fetchall()
    for channel in channels:
        channel_id = int(channel["channel_id"])
        override_rows = await (await conn.execute(
            "SELECT template_key,custom_text FROM channel_template_overrides WHERE channel_id=? ORDER BY template_key",
            (channel_id,),
        )).fetchall()
        overrides = {str(row["template_key"]): str(row["custom_text"]) for row in override_rows}

        revision_cursor = await conn.execute(
            """INSERT INTO channel_custom_revisions(
                   channel_id,source,source_standard_revision_id,created_at,created_by,summary
               ) VALUES(?,?,?,?,NULL,?)""",
            (channel_id, "migration_snapshot", standard_revision_id, now,
             "Initial channel customization snapshot preserving current effective templates"),
        )
        revision_id = int(revision_cursor.lastrowid)
        channel_items = []
        known_keys = set()
        for key, spec in sorted(TEMPLATE_REGISTRY.items()):
            known_keys.add(key)
            effective_text = overrides.get(key, spec.default)
            channel_items.append((
                revision_id,
                f"template:{key}",
                CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                _custom_text_payload(text=effective_text, scope=spec.scope),
            ))
        # Unknown/stale legacy overrides are not rendered by the current
        # registry, but retaining them in the snapshot avoids silent data loss.
        for key in sorted(set(overrides) - known_keys):
            channel_items.append((
                revision_id,
                f"template:{key}",
                CUSTOM_ITEM_TYPE_LEGACY_TEMPLATE_OVERRIDE,
                _custom_text_payload(text=overrides[key], scope="unknown"),
            ))
        if channel_items:
            await conn.executemany(
                "INSERT INTO channel_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                channel_items,
            )
        await conn.execute(
            """INSERT INTO channel_custom_state(
                   channel_id,active_revision_id,initial_revision_id,source_standard_revision_id,updated_at,updated_by
               ) VALUES(?,?,?,?,?,NULL)""",
            (channel_id, revision_id, revision_id, standard_revision_id, now),
        )
        await conn.execute(
            """INSERT INTO customization_audit_log(
                   actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
               ) VALUES(NULL,'channel_custom',?,?, 'migration_seed',NULL,?,?)""",
            (channel_id, channel_id,
             json.dumps({
                 "revision_id": revision_id,
                 "source_standard_revision_id": standard_revision_id,
                 "items": len(channel_items),
                 "legacy_overrides": len(overrides),
             }, sort_keys=True), now),
        )

async def apply_channel_start_card_media(conn: aiosqlite.Connection) -> None:
    """Persist channel-scoped media for the post-Start welcome card.

    The card text continues to use the existing ``start.greeting`` template,
    which is already channel-scoped.  Media is deliberately stored separately
    so it never mutates the global Telegram profile / Description Picture.
    """
    await conn.execute("""CREATE TABLE channel_start_card_media (
        channel_id INTEGER PRIMARY KEY,
        media_type TEXT NOT NULL CHECK(media_type IN ('photo','video','animation')),
        media_file_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")

    # Migration 23 seeded the Standard Pack from the registry that existed at
    # that time. Version 24 adds new start-card UI keys. Keep immutable
    # standard revisions immutable by creating a successor revision containing
    # any newly introduced application defaults instead of mutating the old
    # revision in place. Existing channel snapshots are intentionally untouched.
    from templates import TEMPLATE_REGISTRY

    state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if state is not None:
        previous_revision_id = int(state["active_revision_id"])
        rows = await (await conn.execute(
            "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
            (previous_revision_id,),
        )).fetchall()
        existing_keys = {str(row["item_key"]) for row in rows}
        missing = [
            (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
            if f"template:{key}" not in existing_keys
        ]
        if missing:
            now = dt_to_db(utc_now())
            revision_cursor = await conn.execute(
                "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
                (now, "schema_v24_defaults", "Add application defaults introduced with channel start cards"),
            )
            revision_id = int(revision_cursor.lastrowid)
            await conn.execute(
                """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
                   SELECT ?,item_key,item_type,payload_json
                   FROM bot_standard_custom_items WHERE revision_id=?""",
                (revision_id, previous_revision_id),
            )
            await conn.executemany(
                "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                [
                    (revision_id, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                     _custom_text_payload(text=spec.default, scope=spec.scope))
                    for key, spec in missing
                ],
            )
            await conn.execute(
                "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
                (revision_id, now),
            )
            await conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
                (json.dumps({
                    "previous_revision_id": previous_revision_id,
                    "revision_id": revision_id,
                    "added_items": len(missing),
                }, sort_keys=True), now),
            )


async def apply_template_surface_v25(conn: aiosqlite.Connection) -> None:
    """Extend only the global Standard Pack with Stage-6 template defaults.

    Standard revisions are immutable. Existing channel snapshots must remain
    byte-for-byte independent from later application defaults, so this
    migration creates a successor Standard revision only when registry keys
    are missing and never updates ``channel_custom_*`` rows.
    """
    from templates import TEMPLATE_REGISTRY

    state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if state is None:
        return

    previous_revision_id = int(state["active_revision_id"])
    rows = await (await conn.execute(
        "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
        (previous_revision_id,),
    )).fetchall()
    existing_keys = {str(row["item_key"]) for row in rows}
    missing = [
        (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
        if f"template:{key}" not in existing_keys
    ]
    if not missing:
        return

    now = dt_to_db(utc_now())
    revision_cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "schema_v25_defaults", "Add channel template surface introduced in Stage 6"),
    )
    revision_id = int(revision_cursor.lastrowid)
    await conn.execute(
        """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
           SELECT ?,item_key,item_type,payload_json
           FROM bot_standard_custom_items WHERE revision_id=?""",
        (revision_id, previous_revision_id),
    )
    await conn.executemany(
        "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
        [
            (revision_id, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
             _custom_text_payload(text=spec.default, scope=spec.scope))
            for key, spec in missing
        ],
    )
    await conn.execute(
        "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
        (revision_id, now),
    )
    await conn.execute(
        """INSERT INTO customization_audit_log(
               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
           ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
        (json.dumps({
            "previous_revision_id": previous_revision_id,
            "revision_id": revision_id,
            "added_items": len(missing),
            "schema_version": 25,
        }, sort_keys=True), now),
    )


async def apply_custom_drafts_v26(conn: aiosqlite.Connection) -> None:
    """Add persistent per-channel drafts and consolidate the Stage-6 live overlay.

    The migration preserves the exact effective live text/media state before the
    new editor starts writing only to drafts. Legacy template overrides are
    folded into a successor immutable revision and then cleared. Start-card
    media is also represented inside the immutable revision while the legacy
    table remains as a compatibility mirror for older runtime helpers.
    """
    await conn.execute("""CREATE TABLE channel_custom_drafts (
        channel_id INTEGER PRIMARY KEY,
        base_revision_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE,
        FOREIGN KEY(base_revision_id, channel_id)
            REFERENCES channel_custom_revisions(revision_id, channel_id)
    )""")
    await conn.execute("""CREATE TABLE channel_custom_draft_items (
        channel_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('set','delete')),
        item_type TEXT,
        payload_json TEXT,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        PRIMARY KEY(channel_id, item_key),
        FOREIGN KEY(channel_id) REFERENCES channel_custom_drafts(channel_id) ON DELETE CASCADE,
        CHECK(
            (operation='set' AND item_type IS NOT NULL AND payload_json IS NOT NULL)
            OR (operation='delete' AND item_type IS NULL AND payload_json IS NULL)
        )
    )""")
    await conn.execute(
        "CREATE INDEX idx_channel_custom_draft_items_channel ON channel_custom_draft_items(channel_id,item_key)"
    )

    # Stage-7 introduces owner-facing draft/publish messages. Extend only the
    # global Standard Pack; existing channel snapshots stay independent.
    from templates import TEMPLATE_REGISTRY
    standard_state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if standard_state is not None:
        previous_standard_revision = int(standard_state["active_revision_id"])
        standard_rows = await (await conn.execute(
            "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
            (previous_standard_revision,),
        )).fetchall()
        existing_standard_keys = {str(row["item_key"]) for row in standard_rows}
        missing = [
            (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
            if f"template:{key}" not in existing_standard_keys
        ]
        if missing:
            now = dt_to_db(utc_now())
            cursor = await conn.execute(
                "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
                (now, "schema_v26_defaults", "Add draft/publish UI introduced in Stage 7"),
            )
            new_standard_revision = int(cursor.lastrowid)
            await conn.execute(
                """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
                   SELECT ?,item_key,item_type,payload_json
                   FROM bot_standard_custom_items WHERE revision_id=?""",
                (new_standard_revision, previous_standard_revision),
            )
            await conn.executemany(
                "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                [
                    (new_standard_revision, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                     _custom_text_payload(text=spec.default, scope=spec.scope))
                    for key, spec in missing
                ],
            )
            await conn.execute(
                "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
                (new_standard_revision, now),
            )
            await conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
                (json.dumps({
                    "previous_revision_id": previous_standard_revision,
                    "revision_id": new_standard_revision,
                    "added_items": len(missing),
                    "schema_version": 26,
                }, sort_keys=True), now),
            )

    # Freeze the exact pre-v26 effective state into immutable revisions. This
    # makes later live rendering independent from the mutable override table.
    states = await (await conn.execute(
        "SELECT * FROM channel_custom_state ORDER BY channel_id"
    )).fetchall()
    for state in states:
        channel_id = int(state["channel_id"])
        active_revision_id = int(state["active_revision_id"])
        item_rows = await (await conn.execute(
            "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=? ORDER BY item_key",
            (active_revision_id,),
        )).fetchall()
        items = {
            str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
            for row in item_rows
        }
        changed_keys: list[str] = []

        override_rows = await (await conn.execute(
            "SELECT template_key,custom_text FROM channel_template_overrides WHERE channel_id=? ORDER BY template_key",
            (channel_id,),
        )).fetchall()
        for row in override_rows:
            key = str(row["template_key"])
            spec = TEMPLATE_REGISTRY.get(key)
            item_key = f"template:{key}"
            item_type = CUSTOM_ITEM_TYPE_TEMPLATE_TEXT if spec is not None else CUSTOM_ITEM_TYPE_LEGACY_TEMPLATE_OVERRIDE
            payload = _custom_text_payload(
                text=str(row["custom_text"]), scope=spec.scope if spec is not None else "unknown"
            )
            if items.get(item_key) != (item_type, payload):
                items[item_key] = (item_type, payload)
                changed_keys.append(item_key)

        media = await (await conn.execute(
            "SELECT media_type,media_file_id FROM channel_start_card_media WHERE channel_id=?",
            (channel_id,),
        )).fetchone()
        if media is not None:
            payload = json.dumps(
                {"media_type": str(media["media_type"]), "media_file_id": str(media["media_file_id"])},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            if items.get("start_card.media") != (CUSTOM_ITEM_TYPE_START_CARD_MEDIA, payload):
                items["start_card.media"] = (CUSTOM_ITEM_TYPE_START_CARD_MEDIA, payload)
                changed_keys.append("start_card.media")

        if changed_keys:
            now = dt_to_db(utc_now())
            cursor = await conn.execute(
                """INSERT INTO channel_custom_revisions(
                       channel_id,source,source_standard_revision_id,created_at,created_by,summary
                   ) VALUES(?,?,?,?,NULL,?)""",
                (
                    channel_id,
                    "stage7_live_snapshot",
                    state["source_standard_revision_id"],
                    now,
                    "Consolidate live template/media state before draft-only editing",
                ),
            )
            revision_id = int(cursor.lastrowid)
            if items:
                await conn.executemany(
                    "INSERT INTO channel_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                    [(revision_id, key, item_type, payload) for key, (item_type, payload) in sorted(items.items())],
                )
            await conn.execute(
                "UPDATE channel_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE channel_id=?",
                (revision_id, now, channel_id),
            )
            await conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(NULL,'channel_custom',?,?, 'draft_foundation_migration',NULL,?,?)""",
                (
                    channel_id, channel_id,
                    json.dumps({
                        "previous_revision_id": active_revision_id,
                        "revision_id": revision_id,
                        "changed_keys": sorted(set(changed_keys)),
                        "schema_version": 26,
                    }, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        # This table remains only for compatibility with old migrations/tests;
        # after v26 it is no longer the live owner-edit surface.
        await conn.execute(
            "DELETE FROM channel_template_overrides WHERE channel_id=?", (channel_id,)
        )


async def apply_custom_history_v27(conn: aiosqlite.Connection) -> None:
    """Add rollback intent metadata to drafts and extend Standard UI defaults.

    Existing channel revisions remain immutable and untouched. Existing drafts
    become normal manual-publish drafts. A revision restore can later mark a
    fresh draft as ``rollback`` so publication creates a new rollback revision
    instead of rewriting history.
    """
    await conn.execute(
        "ALTER TABLE channel_custom_drafts ADD COLUMN publish_source TEXT NOT NULL DEFAULT 'manual_publish'"
    )
    await conn.execute(
        "ALTER TABLE channel_custom_drafts ADD COLUMN publish_summary TEXT"
    )
    await conn.execute(
        "ALTER TABLE channel_custom_drafts ADD COLUMN restore_revision_id INTEGER"
    )

    from templates import TEMPLATE_REGISTRY
    standard_state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if standard_state is None:
        return
    previous_revision = int(standard_state["active_revision_id"])
    rows = await (await conn.execute(
        "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
        (previous_revision,),
    )).fetchall()
    existing = {str(row["item_key"]) for row in rows}
    missing = [
        (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
        if f"template:{key}" not in existing
    ]
    if not missing:
        return

    now = dt_to_db(utc_now())
    cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "schema_v27_defaults", "Add revision history, audit and rollback UI defaults"),
    )
    revision_id = int(cursor.lastrowid)
    await conn.execute(
        """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
           SELECT ?,item_key,item_type,payload_json
           FROM bot_standard_custom_items WHERE revision_id=?""",
        (revision_id, previous_revision),
    )
    await conn.executemany(
        "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
        [
            (revision_id, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
             _custom_text_payload(text=spec.default, scope=spec.scope))
            for key, spec in missing
        ],
    )
    await conn.execute(
        "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
        (revision_id, now),
    )
    await conn.execute(
        """INSERT INTO customization_audit_log(
               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
           ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
        (json.dumps({
            "previous_revision_id": previous_revision,
            "revision_id": revision_id,
            "added_items": len(missing),
            "schema_version": 27,
        }, sort_keys=True), now),
    )


async def apply_custom_tools_v28(conn: aiosqlite.Connection) -> None:
    """Add bulk customization draft provenance and Stage-9 UI defaults.

    Existing channel live revisions remain untouched.  New nullable metadata on
    drafts records where a bulk reset/apply/copy operation came from so the
    resulting immutable revision and audit log preserve useful provenance.
    """
    await conn.execute(
        "ALTER TABLE channel_custom_drafts ADD COLUMN source_channel_id INTEGER"
    )
    await conn.execute(
        "ALTER TABLE channel_custom_drafts ADD COLUMN source_standard_revision_id INTEGER"
    )

    from templates import TEMPLATE_REGISTRY
    standard_state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if standard_state is None:
        return
    previous_revision = int(standard_state["active_revision_id"])
    rows = await (await conn.execute(
        "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
        (previous_revision,),
    )).fetchall()
    existing = {str(row["item_key"]) for row in rows}
    missing = [
        (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
        if f"template:{key}" not in existing
    ]
    if not missing:
        return

    now = dt_to_db(utc_now())
    cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "schema_v28_defaults", "Add reset, current-standard and own-channel copy UI defaults"),
    )
    revision_id = int(cursor.lastrowid)
    await conn.execute(
        """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
           SELECT ?,item_key,item_type,payload_json
           FROM bot_standard_custom_items WHERE revision_id=?""",
        (revision_id, previous_revision),
    )
    await conn.executemany(
        "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
        [
            (revision_id, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
             _custom_text_payload(text=spec.default, scope=spec.scope))
            for key, spec in missing
        ],
    )
    await conn.execute(
        "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
        (revision_id, now),
    )
    await conn.execute(
        """INSERT INTO customization_audit_log(
               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
           ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
        (json.dumps({
            "previous_revision_id": previous_revision,
            "revision_id": revision_id,
            "added_items": len(missing),
            "schema_version": 28,
        }, sort_keys=True), now),
    )


async def apply_custom_transfer_v29(conn: aiosqlite.Connection) -> None:
    """Add Stage-10 import/export UI defaults without touching channel snapshots.

    Export/import needs no new mutable persistence: existing drafts, immutable
    revisions and audit rows already provide the required safety model.  This
    migration only advances the current Standard Custom Pack when new
    channel-scoped UI/template keys were introduced by Stage 10. Existing
    channels intentionally keep their prior immutable snapshots.
    """
    from templates import TEMPLATE_REGISTRY

    standard_state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if standard_state is None:
        return
    previous_revision = int(standard_state["active_revision_id"])
    rows = await (await conn.execute(
        "SELECT item_key FROM bot_standard_custom_items WHERE revision_id=?",
        (previous_revision,),
    )).fetchall()
    existing = {str(row["item_key"]) for row in rows}
    missing = [
        (key, spec) for key, spec in sorted(TEMPLATE_REGISTRY.items())
        if f"template:{key}" not in existing
    ]
    if not missing:
        return

    now = dt_to_db(utc_now())
    cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "schema_v29_defaults", "Add safe Channel Custom Pack import/export UI defaults"),
    )
    revision_id = int(cursor.lastrowid)
    await conn.execute(
        """INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json)
           SELECT ?,item_key,item_type,payload_json
           FROM bot_standard_custom_items WHERE revision_id=?""",
        (revision_id, previous_revision),
    )
    await conn.executemany(
        "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
        [
            (revision_id, f"template:{key}", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
             _custom_text_payload(text=spec.default, scope=spec.scope))
            for key, spec in missing
        ],
    )
    await conn.execute(
        "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
        (revision_id, now),
    )
    await conn.execute(
        """INSERT INTO customization_audit_log(
               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
           ) VALUES(NULL,'global_standard',1,NULL,'schema_defaults_added',NULL,?,?)""",
        (json.dumps({
            "previous_revision_id": previous_revision,
            "revision_id": revision_id,
            "added_items": len(missing),
            "schema_version": 29,
        }, sort_keys=True), now),
    )


async def apply_standard_global_separation_v30(conn: aiosqlite.Connection) -> None:
    """Make the active Standard Custom Pack strictly channel-scoped.

    Earlier foundation migrations intentionally mirrored the whole template
    registry into the Standard Pack, including global setup/pre-start texts.
    Stage 11 finalizes the model: global bot-profile UI stays application/global
    state, while the active Standard revision contains only channel-scoped
    templates plus the optional default Channel Start Card media item.

    Historical Standard revisions and every existing Channel Custom Pack remain
    immutable and untouched.
    """
    from templates import TEMPLATE_REGISTRY

    state = await (await conn.execute(
        "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
    )).fetchone()
    if state is None:
        return
    previous_revision = int(state["active_revision_id"])
    rows = await (await conn.execute(
        "SELECT item_key,item_type,payload_json FROM bot_standard_custom_items WHERE revision_id=? ORDER BY item_key",
        (previous_revision,),
    )).fetchall()

    allowed_template_keys = {
        f"template:{key}" for key, spec in TEMPLATE_REGISTRY.items() if spec.scope == "channel"
    }
    kept: list[tuple[str, str, str]] = []
    removed: list[str] = []
    for row in rows:
        item_key = str(row["item_key"])
        if item_key in allowed_template_keys or item_key == "start_card.media":
            kept.append((item_key, str(row["item_type"]), str(row["payload_json"])))
        else:
            removed.append(item_key)

    # Even if a legacy fixture is already clean, record no redundant revision.
    if not removed:
        return

    now = dt_to_db(utc_now())
    cursor = await conn.execute(
        "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,NULL,?,?)",
        (now, "schema_v30_scope_separation", "Remove global bot/profile items from active Standard Custom Pack"),
    )
    revision_id = int(cursor.lastrowid)
    if kept:
        await conn.executemany(
            "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
            [(revision_id, item_key, item_type, payload_json) for item_key, item_type, payload_json in kept],
        )
    await conn.execute(
        "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=NULL WHERE singleton_id=1",
        (revision_id, now),
    )
    await conn.execute(
        """INSERT INTO customization_audit_log(
               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
           ) VALUES(NULL,'global_standard',1,NULL,'standard_scope_separated',NULL,?,?)""",
        (json.dumps({
            "previous_revision_id": previous_revision,
            "revision_id": revision_id,
            "removed_items": len(removed),
            "removed_keys": removed,
            "schema_version": 30,
        }, ensure_ascii=False, sort_keys=True), now),
    )


async def apply_anonymous_cycle_state(conn: aiosqlite.Connection) -> None:
    """Decouple anonymous numbering cycles from cleanup schedule edits."""
    await conn.execute("ALTER TABLE channel_anonymous_counters RENAME TO legacy_channel_anonymous_counters")
    await conn.execute("""CREATE TABLE channel_anonymous_counters (
        channel_id INTEGER PRIMARY KEY,
        next_number INTEGER NOT NULL DEFAULT 1 CHECK (next_number >= 1),
        cycle_key TEXT NOT NULL,
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("""INSERT INTO channel_anonymous_counters(channel_id,next_number,cycle_key)
        SELECT old.channel_id, old.next_number, c.next_reset_at
        FROM legacy_channel_anonymous_counters old
        JOIN channels c ON c.channel_id=old.channel_id""")
    await conn.execute("DROP TABLE legacy_channel_anonymous_counters")


async def apply_bot_prestart_card(conn: aiosqlite.Connection) -> None:
    """Persist the bot-wide pre-Start card draft without copying defaults."""
    await conn.execute("""CREATE TABLE bot_prestart_card (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        description_override TEXT,
        media_type TEXT CHECK (media_type IS NULL OR media_type IN ('photo','video','animation')),
        media_file_id TEXT,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL,
        CHECK ((media_type IS NULL AND media_file_id IS NULL) OR (media_type IS NOT NULL AND media_file_id IS NOT NULL))
    )""")


async def apply_reaction_routing(conn: aiosqlite.Connection) -> None:
    """Persist per-channel reaction mode, source mapping and idempotency journals."""
    await conn.execute("""CREATE TABLE channel_reaction_settings (
        channel_id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL DEFAULT 'subscriber' CHECK (mode IN ('subscriber','service')),
        service_topic_id INTEGER,
        service_topic_name TEXT,
        requires_repair INTEGER NOT NULL DEFAULT 0 CHECK (requires_repair IN (0,1)),
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("""CREATE TABLE channel_reaction_sources (
        channel_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        forum_message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        privacy_mode TEXT NOT NULL CHECK (privacy_mode IN ('identified','anonymous')),
        private_chat_id INTEGER NOT NULL,
        private_message_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(group_id, forum_message_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_reaction_sources_channel_topic ON channel_reaction_sources(channel_id,topic_id)")
    await conn.execute("""CREATE TABLE channel_reaction_events (
        channel_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        actor_id INTEGER NOT NULL,
        reaction_key TEXT NOT NULL,
        event_at TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('subscriber','service')),
        created_at TEXT NOT NULL,
        PRIMARY KEY(channel_id,group_id,source_message_id,actor_id,reaction_key,event_at),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("""CREATE TABLE channel_reaction_dispatches (
        channel_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        service_topic_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('sending','sent','error')),
        triggered_by INTEGER NOT NULL,
        reaction_key TEXT NOT NULL,
        destination_message_id INTEGER,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(channel_id,group_id,source_message_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")


async def apply_broadcast_album_sources(conn: aiosqlite.Connection) -> None:
    """Allow one broadcast draft to persist a complete Telegram media group."""
    await conn.execute("ALTER TABLE channel_broadcasts ADD COLUMN source_message_ids TEXT")
    await conn.execute("ALTER TABLE channel_broadcasts ADD COLUMN source_media_group_id TEXT")


async def apply_mass_broadcasts(conn: aiosqlite.Connection) -> None:
    """Persist idempotent per-channel mass broadcast state and delivery journal."""
    await conn.execute("""CREATE TABLE channel_broadcasts (
        broadcast_id TEXT PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        created_by INTEGER NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft','sending','completed','cancelled')),
        recipient_count INTEGER NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(broadcast_id, channel_id),
        FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("""CREATE TABLE channel_broadcast_deliveries (
        broadcast_id TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        privacy_mode TEXT CHECK (privacy_mode IS NULL OR privacy_mode IN ('identified','anonymous')),
        topic_id INTEGER,
        status TEXT NOT NULL CHECK (status IN ('pending','reserved','delivered','undelivered','error')),
        error_code TEXT,
        reserved_at TEXT,
        completed_at TEXT,
        PRIMARY KEY(broadcast_id, channel_id, user_id),
        FOREIGN KEY(broadcast_id, channel_id) REFERENCES channel_broadcasts(broadcast_id, channel_id) ON DELETE CASCADE
    )""")
    await conn.execute("CREATE INDEX idx_broadcast_deliveries_status ON channel_broadcast_deliveries(broadcast_id,channel_id,status)")
    await conn.execute("CREATE UNIQUE INDEX idx_one_sending_broadcast_per_channel ON channel_broadcasts(channel_id) WHERE status='sending'")


DEFAULT_MIGRATIONS = (
    Migration(1, "baseline_legacy_schema", apply_legacy_schema),
    Migration(2, "channel_model", apply_channel_model),
    Migration(3, "subscriber_privacy", apply_privacy_model),
    Migration(4, "message_event_log", apply_message_event_log),
    Migration(5, "topic_statuses", apply_topic_statuses),
    Migration(6, "topic_cleanup_protection", apply_topic_cleanup_protection),
    Migration(7, "channel_cleanup_policy", apply_channel_cleanup_policy),
    Migration(8, "active_admin_channel", apply_active_admin_channel),
    Migration(9, "channel_topic_templates", apply_channel_topic_templates),
    Migration(10, "subscriber_moderation_state", apply_subscriber_moderation_state),
    Migration(11, "moderation_log", apply_moderation_log),
    Migration(12, "sanction_reasons", apply_sanction_reasons),
    Migration(13, "subscriber_sanctions", apply_subscriber_sanctions),
    Migration(14, "subscriber_metadata", apply_subscriber_metadata),
    Migration(15, "subscriber_metadata_management", apply_subscriber_metadata_management),
    Migration(16, "subscriber_statistics_events", apply_subscriber_statistics_events),
    Migration(17, "channel_template_overrides", apply_channel_template_overrides),
    Migration(18, "anonymous_cycle_state", apply_anonymous_cycle_state),
    Migration(19, "bot_prestart_card", apply_bot_prestart_card),
    Migration(20, "mass_broadcasts", apply_mass_broadcasts),
    Migration(21, "reaction_routing", apply_reaction_routing),
    Migration(22, "broadcast_album_sources", apply_broadcast_album_sources),
    Migration(23, "custom_pack_foundation", apply_custom_pack_foundation),
    Migration(24, "channel_start_card_media", apply_channel_start_card_media),
    Migration(25, "template_surface_v25", apply_template_surface_v25),
    Migration(26, "channel_custom_drafts", apply_custom_drafts_v26),
    Migration(27, "custom_history_and_rollback", apply_custom_history_v27),
    Migration(28, "custom_tools_and_provenance", apply_custom_tools_v28),
    Migration(29, "custom_transfer_json", apply_custom_transfer_v29),
    Migration(30, "standard_global_separation", apply_standard_global_separation_v30),
)
CURRENT_SCHEMA_VERSION = DEFAULT_MIGRATIONS[-1].version

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
        backup_dir: str | Path | None = None,
        backup_keep: int = 7,
        backup_manager: SQLiteBackupManager | None = None,
    ) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self.applied_migration_versions: tuple[int, ...] = ()
        self._migrations = tuple(
            DEFAULT_MIGRATIONS if migrations is None else migrations
        )
        resolved_backup_dir = (
            Path(backup_dir)
            if backup_dir is not None
            else Path(path).parent / "backups"
        )
        self._backup_manager = backup_manager or SQLiteBackupManager(
            source_path=path,
            backup_dir=resolved_backup_dir,
            keep=backup_keep,
        )
        self._validate_migrations()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() must be called first")
        return self._conn

    async def inspect_pending_migrations(self) -> tuple[Migration, ...]:
        """Read-only release validation: never applies or records migrations."""
        if self._conn is not None:
            raise RuntimeError("Database connection is already open")
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        try:
            await self.conn.execute("PRAGMA foreign_keys = ON")
            await self.conn.execute("PRAGMA busy_timeout = 5000")
            await self.run_preflight()
            history, _ = await self._load_migration_history()
            self._validate_migration_history(history)
            if not history:
                existing = await self._existing_legacy_tables()
                if existing:
                    await self._validate_legacy_schema(existing)
            return self._pending_migrations(history)
        finally:
            await self.close()

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
            history, has_migration_table = await self._load_migration_history()
            self._validate_migration_history(history)
            pending = self._pending_migrations(history)
            if pending and not await self._is_new_database():
                await self._backup_before_migrations(pending)
            if not has_migration_table:
                await self._ensure_migration_table()
            else:
                await self._validate_migration_table()
            await self._apply_pending_migrations(history)
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

    async def _apply_pending_migrations(
        self,
        history: Sequence[tuple[int, str]],
    ) -> None:
        applied_versions = {version for version, _ in history}

        newly_applied: list[int] = []
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
                newly_applied.append(migration.version)
            except Exception:
                await self.conn.rollback()
                raise
        self.applied_migration_versions = tuple(newly_applied)

    async def _load_migration_history(
        self,
    ) -> tuple[list[tuple[int, str]], bool]:
        if not await self._table_exists("schema_migrations"):
            return [], False
        await self._validate_migration_table()
        return await self._read_migration_history(), True

    def _pending_migrations(
        self,
        history: Sequence[tuple[int, str]],
    ) -> tuple[Migration, ...]:
        applied_versions = {version for version, _ in history}
        return tuple(
            migration
            for migration in self._migrations
            if migration.version not in applied_versions
        )

    async def _is_new_database(self) -> bool:
        cursor = await self.conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        )
        return await cursor.fetchone() is None

    async def _backup_before_migrations(
        self,
        pending: Sequence[Migration],
    ) -> None:
        backup_path = await self._backup_manager.create_backup(
            self.conn,
            target_version=pending[-1].version,
        )
        self._backup_manager.verify_backup(backup_path)
        self._backup_manager.rotate_after_success()

    async def _table_exists(self, table: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return await cursor.fetchone() is not None

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
    # Per-channel UI template overrides. Defaults live in templates.py and are
    # never copied to the database.
    # ------------------------------------------------------------------
    async def get_template_override(self, *, channel_id: int, template_key: str) -> str | None:
        row = await (await self.conn.execute("SELECT custom_text FROM channel_template_overrides WHERE channel_id=? AND template_key=?", (channel_id, template_key))).fetchone()
        return None if row is None else str(row["custom_text"])

    async def set_template_override(self, *, channel_id: int, template_key: str, custom_text: str, updated_by: int) -> None:
        async with self._write_lock:
            await self.conn.execute("INSERT INTO channel_template_overrides(channel_id,template_key,custom_text,updated_at,updated_by) VALUES(?,?,?,?,?) ON CONFLICT(channel_id,template_key) DO UPDATE SET custom_text=excluded.custom_text,updated_at=excluded.updated_at,updated_by=excluded.updated_by", (channel_id, template_key, custom_text, dt_to_db(utc_now()), updated_by))
            await self.conn.commit()

    async def reset_template_override(self, *, channel_id: int, template_key: str) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute("DELETE FROM channel_template_overrides WHERE channel_id=? AND template_key=?", (channel_id, template_key))
            await self.conn.commit()
            return cursor.rowcount > 0

    async def reset_all_template_overrides(self, *, channel_id: int) -> int:
        async with self._write_lock:
            cursor = await self.conn.execute("DELETE FROM channel_template_overrides WHERE channel_id=?", (channel_id,))
            await self.conn.commit()
            return cursor.rowcount

    async def list_template_override_keys(self, *, channel_id: int) -> set[str]:
        rows = await (await self.conn.execute("SELECT template_key FROM channel_template_overrides WHERE channel_id=? ORDER BY template_key", (channel_id,))).fetchall()
        return {str(row["template_key"]) for row in rows}


    # ------------------------------------------------------------------
    # Custom Pack foundation (schema v23).
    # Rendering still uses channel_template_overrides in this stage.  These
    # APIs expose immutable standard/channel snapshots and an optional overlay
    # of the legacy live overrides so subsequent stages can migrate safely.
    # ------------------------------------------------------------------
    async def get_standard_custom_state(self) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM bot_standard_custom_state WHERE singleton_id=1"
        )).fetchone()

    async def get_standard_custom_items(self, *, revision_id: int | None = None) -> dict[str, dict[str, object]]:
        if revision_id is None:
            state = await self.get_standard_custom_state()
            if state is None:
                return {}
            revision_id = int(state["active_revision_id"])
        rows = await (await self.conn.execute(
            "SELECT item_key,item_type,payload_json FROM bot_standard_custom_items WHERE revision_id=? ORDER BY item_key",
            (revision_id,),
        )).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {"raw": str(row["payload_json"])}
            result[str(row["item_key"])] = {
                "item_type": str(row["item_type"]),
                "payload": payload,
            }
        return result

    async def get_standard_custom_revision(self, revision_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM bot_standard_custom_revisions WHERE revision_id=?", (revision_id,)
        )).fetchone()

    async def list_standard_custom_revisions(self, *, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        return await (await self.conn.execute(
            """SELECT revision_id,created_at,created_by,source,summary
               FROM bot_standard_custom_revisions
               ORDER BY revision_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )).fetchall()

    async def count_standard_custom_revisions(self) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) AS n FROM bot_standard_custom_revisions"
        )).fetchone()
        return 0 if row is None else int(row["n"])

    async def get_standard_custom_template_text(
        self, *, template_key: str, revision_id: int | None = None
    ) -> str | None:
        from templates import TEMPLATE_REGISTRY

        spec = TEMPLATE_REGISTRY.get(template_key)
        if spec is None or spec.scope != "channel":
            return None
        if revision_id is None:
            state = await self.get_standard_custom_state()
            if state is None:
                return None
            revision_id = int(state["active_revision_id"])
        row = await (await self.conn.execute(
            """SELECT item_type,payload_json FROM bot_standard_custom_items
               WHERE revision_id=? AND item_key=?""",
            (revision_id, f"template:{template_key}"),
        )).fetchone()
        if row is None or str(row["item_type"]) != CUSTOM_ITEM_TYPE_TEMPLATE_TEXT:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        text = payload.get("text") if isinstance(payload, dict) else None
        return text if isinstance(text, str) else None

    async def get_standard_custom_start_card_media(
        self, *, revision_id: int | None = None
    ) -> dict[str, str] | None:
        if revision_id is None:
            state = await self.get_standard_custom_state()
            if state is None:
                return None
            revision_id = int(state["active_revision_id"])
        row = await (await self.conn.execute(
            """SELECT item_type,payload_json FROM bot_standard_custom_items
               WHERE revision_id=? AND item_key='start_card.media'""",
            (revision_id,),
        )).fetchone()
        if row is None or str(row["item_type"]) != CUSTOM_ITEM_TYPE_START_CARD_MEDIA:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        media_type = payload.get("media_type")
        media_file_id = payload.get("media_file_id")
        if media_type not in {"photo", "video", "animation"} or not isinstance(media_file_id, str) or not media_file_id:
            return None
        return {"media_type": str(media_type), "media_file_id": media_file_id}

    async def _publish_standard_change_locked(
        self,
        *,
        actor_id: int,
        changes: dict[str, tuple[str, str] | None],
        source: str,
        summary: str,
        audit_action: str,
        target_key: str | None,
        audit_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        state = await (await self.conn.execute(
            "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
        )).fetchone()
        if state is None:
            raise RuntimeError("Standard Custom Pack is unavailable")
        previous_revision = int(state["active_revision_id"])
        current_rows = await (await self.conn.execute(
            "SELECT item_key,item_type,payload_json FROM bot_standard_custom_items WHERE revision_id=?",
            (previous_revision,),
        )).fetchall()
        current = {
            str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
            for row in current_rows
        }
        effective = dict(current)
        for item_key, value in changes.items():
            if value is None:
                effective.pop(item_key, None)
            else:
                effective[item_key] = value
        if effective == current:
            return {"revision_id": previous_revision, "previous_revision_id": previous_revision, "changed": False}

        now = dt_to_db(utc_now())
        cursor = await self.conn.execute(
            "INSERT INTO bot_standard_custom_revisions(created_at,created_by,source,summary) VALUES(?,?,?,?)",
            (now, actor_id, source, summary),
        )
        revision_id = int(cursor.lastrowid)
        if effective:
            await self.conn.executemany(
                "INSERT INTO bot_standard_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                [(revision_id, key, value[0], value[1]) for key, value in sorted(effective.items())],
            )
        await self.conn.execute(
            "UPDATE bot_standard_custom_state SET active_revision_id=?,updated_at=?,updated_by=? WHERE singleton_id=1",
            (revision_id, now, actor_id),
        )
        metadata = {
            "previous_revision_id": previous_revision,
            "revision_id": revision_id,
            "changed_keys": sorted(changes),
        }
        if audit_metadata:
            metadata.update(audit_metadata)
        await self.conn.execute(
            """INSERT INTO customization_audit_log(
                   actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
               ) VALUES(?,'global_standard',1,NULL,?,?,?,?)""",
            (actor_id, audit_action, target_key, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now),
        )
        return {"revision_id": revision_id, "previous_revision_id": previous_revision, "changed": True}

    async def publish_standard_custom_template_text(
        self, *, template_key: str, custom_text: str, updated_by: int
    ) -> dict[str, object]:
        from templates import TEMPLATE_REGISTRY, validate_template

        spec = TEMPLATE_REGISTRY.get(template_key)
        if spec is None or spec.scope != "channel":
            raise ValueError("Only channel-scoped templates belong to the Standard Custom Pack")
        validate_template(template_key, custom_text)
        payload = _custom_text_payload(text=custom_text, scope="channel")
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = await self._publish_standard_change_locked(
                    actor_id=updated_by,
                    changes={f"template:{template_key}": (CUSTOM_ITEM_TYPE_TEMPLATE_TEXT, payload)},
                    source="superadmin_edit",
                    summary=f"Update Standard Custom Pack template {template_key}",
                    audit_action="global_standard_changed",
                    target_key=f"template:{template_key}",
                    audit_metadata={"template_key": template_key},
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def reset_standard_custom_template_text(
        self, *, template_key: str, updated_by: int
    ) -> dict[str, object]:
        from templates import TEMPLATE_REGISTRY

        spec = TEMPLATE_REGISTRY.get(template_key)
        if spec is None or spec.scope != "channel":
            raise ValueError("Only channel-scoped templates belong to the Standard Custom Pack")
        return await self.publish_standard_custom_template_text(
            template_key=template_key, custom_text=spec.default, updated_by=updated_by
        )

    async def set_standard_custom_start_card_media(
        self, *, media_type: str, media_file_id: str, updated_by: int
    ) -> dict[str, object]:
        if media_type not in {"photo", "video", "animation"} or not media_file_id:
            raise ValueError("Unsupported Standard Start Card media")
        payload = json.dumps(
            {"media_type": media_type, "media_file_id": media_file_id},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = await self._publish_standard_change_locked(
                    actor_id=updated_by,
                    changes={"start_card.media": (CUSTOM_ITEM_TYPE_START_CARD_MEDIA, payload)},
                    source="superadmin_edit",
                    summary="Update Standard Channel Start Card media",
                    audit_action="global_standard_changed",
                    target_key="start_card.media",
                    audit_metadata={"media_type": media_type},
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def remove_standard_custom_start_card_media(self, *, updated_by: int) -> dict[str, object]:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = await self._publish_standard_change_locked(
                    actor_id=updated_by,
                    changes={"start_card.media": None},
                    source="superadmin_edit",
                    summary="Remove Standard Channel Start Card media",
                    audit_action="global_standard_changed",
                    target_key="start_card.media",
                    audit_metadata={"operation": "delete"},
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def get_channel_custom_state(self, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_custom_state WHERE channel_id=?",
            (channel_id,),
        )).fetchone()

    async def get_channel_custom_items(
        self,
        *,
        channel_id: int,
        revision_id: int | None = None,
        include_legacy_template_overlay: bool = True,
    ) -> dict[str, dict[str, object]]:
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            return {}
        if revision_id is None:
            revision_id = int(state["active_revision_id"])
        revision = await (await self.conn.execute(
            "SELECT 1 FROM channel_custom_revisions WHERE revision_id=? AND channel_id=?",
            (revision_id, channel_id),
        )).fetchone()
        if revision is None:
            return {}
        rows = await (await self.conn.execute(
            "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=? ORDER BY item_key",
            (revision_id,),
        )).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {"raw": str(row["payload_json"])}
            result[str(row["item_key"])] = {
                "item_type": str(row["item_type"]),
                "payload": payload,
            }

        if include_legacy_template_overlay:
            override_rows = await (await self.conn.execute(
                "SELECT template_key,custom_text FROM channel_template_overrides WHERE channel_id=? ORDER BY template_key",
                (channel_id,),
            )).fetchall()
            from templates import TEMPLATE_REGISTRY
            for row in override_rows:
                key = str(row["template_key"])
                spec = TEMPLATE_REGISTRY.get(key)
                result[f"template:{key}"] = {
                    "item_type": CUSTOM_ITEM_TYPE_TEMPLATE_TEXT if spec is not None else CUSTOM_ITEM_TYPE_LEGACY_TEMPLATE_OVERRIDE,
                    "payload": {
                        "scope": spec.scope if spec is not None else "unknown",
                        "text": str(row["custom_text"]),
                    },
                    "legacy_overlay": True,
                }
        return result

    async def get_channel_custom_template_text(
        self,
        *,
        channel_id: int,
        template_key: str,
        include_legacy_template_overlay: bool = True,
        revision_id: int | None = None,
        include_draft: bool = False,
    ) -> str | None:
        if include_draft:
            draft_text = await self.get_channel_custom_draft_template_text(
                channel_id=channel_id, template_key=template_key
            )
            if draft_text is not None:
                return draft_text
        item = (await self.get_channel_custom_items(
            channel_id=channel_id,
            revision_id=revision_id,
            include_legacy_template_overlay=include_legacy_template_overlay and revision_id is None,
        )).get(f"template:{template_key}")
        if item is None:
            return None
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return None
        value = payload.get("text")
        return value if isinstance(value, str) else None

    async def list_customization_audit(
        self,
        *,
        channel_id: int | None = None,
        scope_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where: list[str] = []
        params: list[object] = []
        if channel_id is not None:
            where.append("channel_id=?")
            params.append(channel_id)
        if scope_type is not None:
            if scope_type not in {"global_standard", "channel_custom", "global_profile"}:
                raise ValueError("Unknown customization audit scope")
            where.append("scope_type=?")
            params.append(scope_type)
        query = "SELECT * FROM customization_audit_log"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY event_id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        return await (await self.conn.execute(query, tuple(params))).fetchall()

    async def count_customization_audit(
        self, *, channel_id: int | None = None, scope_type: str | None = None
    ) -> int:
        where: list[str] = []
        params: list[object] = []
        if channel_id is not None:
            where.append("channel_id=?")
            params.append(channel_id)
        if scope_type is not None:
            if scope_type not in {"global_standard", "channel_custom", "global_profile"}:
                raise ValueError("Unknown customization audit scope")
            where.append("scope_type=?")
            params.append(scope_type)
        query = "SELECT COUNT(*) AS n FROM customization_audit_log"
        if where:
            query += " WHERE " + " AND ".join(where)
        row = await (await self.conn.execute(query, tuple(params))).fetchone()
        return 0 if row is None else int(row["n"])

    async def list_global_customization_audit(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        return await (await self.conn.execute(
            """SELECT * FROM customization_audit_log
               WHERE scope_type IN ('global_standard','global_profile')
               ORDER BY event_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )).fetchall()

    async def count_global_customization_audit(self) -> int:
        row = await (await self.conn.execute(
            """SELECT COUNT(*) AS n FROM customization_audit_log
               WHERE scope_type IN ('global_standard','global_profile')"""
        )).fetchone()
        return 0 if row is None else int(row["n"])

    async def count_channel_custom_revisions(self, channel_id: int) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) AS n FROM channel_custom_revisions WHERE channel_id=?",
            (channel_id,),
        )).fetchone()
        return 0 if row is None else int(row["n"])

    async def list_channel_custom_revisions(
        self, *, channel_id: int, limit: int = 10, offset: int = 0
    ) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        return await (await self.conn.execute(
            """SELECT revision_id,channel_id,source,source_standard_revision_id,
                      created_at,created_by,summary
               FROM channel_custom_revisions
               WHERE channel_id=?
               ORDER BY revision_id DESC LIMIT ? OFFSET ?""",
            (channel_id, limit, offset),
        )).fetchall()

    async def get_channel_custom_revision(
        self, *, channel_id: int, revision_id: int
    ) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            """SELECT revision_id,channel_id,source,source_standard_revision_id,
                      created_at,created_by,summary
               FROM channel_custom_revisions WHERE channel_id=? AND revision_id=?""",
            (channel_id, revision_id),
        )).fetchone()

    async def get_previous_channel_custom_revision_id(
        self, *, channel_id: int, revision_id: int
    ) -> int | None:
        row = await (await self.conn.execute(
            """SELECT revision_id FROM channel_custom_revisions
               WHERE channel_id=? AND revision_id<?
               ORDER BY revision_id DESC LIMIT 1""",
            (channel_id, revision_id),
        )).fetchone()
        return None if row is None else int(row["revision_id"])

    async def diff_channel_custom_revision(
        self, *, channel_id: int, revision_id: int
    ) -> dict[str, object]:
        revision = await self.get_channel_custom_revision(
            channel_id=channel_id, revision_id=revision_id
        )
        if revision is None:
            raise ValueError("Customization revision is unavailable")
        previous_id = await self.get_previous_channel_custom_revision_id(
            channel_id=channel_id, revision_id=revision_id
        )
        current = await self.get_channel_custom_items(
            channel_id=channel_id, revision_id=revision_id,
            include_legacy_template_overlay=False,
        )
        previous = {} if previous_id is None else await self.get_channel_custom_items(
            channel_id=channel_id, revision_id=previous_id,
            include_legacy_template_overlay=False,
        )
        changed = sorted(
            key for key in set(current) | set(previous)
            if current.get(key) != previous.get(key)
        )
        return {
            "revision_id": revision_id,
            "previous_revision_id": previous_id,
            "changed_keys": changed,
            "item_count": len(current),
        }

    # ------------------------------------------------------------------
    # Persistent channel customization drafts (schema v26).
    # ------------------------------------------------------------------
    async def get_channel_custom_draft_state(self, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_custom_drafts WHERE channel_id=?", (channel_id,)
        )).fetchone()

    async def get_channel_custom_draft_items(self, channel_id: int) -> dict[str, dict[str, object]]:
        rows = await (await self.conn.execute(
            """SELECT item_key,operation,item_type,payload_json,updated_at,updated_by
               FROM channel_custom_draft_items WHERE channel_id=? ORDER BY item_key""",
            (channel_id,),
        )).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            payload: object = None
            raw = row["payload_json"]
            if raw is not None:
                try:
                    payload = json.loads(str(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {"raw": str(raw)}
            result[str(row["item_key"])] = {
                "operation": str(row["operation"]),
                "item_type": None if row["item_type"] is None else str(row["item_type"]),
                "payload": payload,
                "updated_at": str(row["updated_at"]),
                "updated_by": int(row["updated_by"]),
            }
        return result

    async def get_channel_custom_draft_count(self, channel_id: int) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) AS n FROM channel_custom_draft_items WHERE channel_id=?",
            (channel_id,),
        )).fetchone()
        return 0 if row is None else int(row["n"])

    async def has_channel_custom_draft(self, channel_id: int) -> bool:
        return await self.get_channel_custom_draft_count(channel_id) > 0

    async def _ensure_custom_draft_locked(self, *, channel_id: int, updated_by: int) -> int:
        state = await (await self.conn.execute(
            "SELECT active_revision_id FROM channel_custom_state WHERE channel_id=?",
            (channel_id,),
        )).fetchone()
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        active_revision_id = int(state["active_revision_id"])
        draft = await (await self.conn.execute(
            "SELECT base_revision_id FROM channel_custom_drafts WHERE channel_id=?",
            (channel_id,),
        )).fetchone()
        if draft is not None:
            if int(draft["base_revision_id"]) != active_revision_id:
                raise DraftConflictError("Draft base revision is stale")
            await self.conn.execute(
                "UPDATE channel_custom_drafts SET updated_at=?,updated_by=? WHERE channel_id=?",
                (dt_to_db(utc_now()), updated_by, channel_id),
            )
            return active_revision_id
        now = dt_to_db(utc_now())
        await self.conn.execute(
            """INSERT INTO channel_custom_drafts(
                   channel_id,base_revision_id,created_at,updated_at,updated_by
               ) VALUES(?,?,?,?,?)""",
            (channel_id, active_revision_id, now, now, updated_by),
        )
        return active_revision_id

    async def _set_custom_draft_item_locked(
        self,
        *,
        channel_id: int,
        item_key: str,
        operation: str,
        item_type: str | None,
        payload_json: str | None,
        updated_by: int,
    ) -> None:
        if operation not in {"set", "delete"}:
            raise ValueError("Unsupported draft operation")
        await self._ensure_custom_draft_locked(channel_id=channel_id, updated_by=updated_by)
        now = dt_to_db(utc_now())
        await self.conn.execute(
            """INSERT INTO channel_custom_draft_items(
                   channel_id,item_key,operation,item_type,payload_json,updated_at,updated_by
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(channel_id,item_key) DO UPDATE SET
                   operation=excluded.operation,
                   item_type=excluded.item_type,
                   payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at,
                   updated_by=excluded.updated_by""",
            (channel_id, item_key, operation, item_type, payload_json, now, updated_by),
        )

    async def set_channel_custom_draft_template_text(
        self, *, channel_id: int, template_key: str, custom_text: str, updated_by: int
    ) -> None:
        from templates import TEMPLATE_REGISTRY, validate_template
        spec = TEMPLATE_REGISTRY.get(template_key)
        if spec is None or spec.scope != "channel":
            raise ValueError("Template is not channel-scoped")
        validate_template(template_key, custom_text)
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self._set_custom_draft_item_locked(
                    channel_id=channel_id,
                    item_key=f"template:{template_key}",
                    operation="set",
                    item_type=CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                    payload_json=_custom_text_payload(text=custom_text, scope=spec.scope),
                    updated_by=updated_by,
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def get_channel_custom_draft_template_text(
        self, *, channel_id: int, template_key: str
    ) -> str | None:
        row = await (await self.conn.execute(
            """SELECT operation,payload_json FROM channel_custom_draft_items
               WHERE channel_id=? AND item_key=?""",
            (channel_id, f"template:{template_key}"),
        )).fetchone()
        if row is None or str(row["operation"]) != "set" or row["payload_json"] is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        value = payload.get("text") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None

    async def list_channel_custom_draft_template_keys(self, channel_id: int) -> set[str]:
        rows = await (await self.conn.execute(
            """SELECT item_key FROM channel_custom_draft_items
               WHERE channel_id=? AND item_key LIKE 'template:%' ORDER BY item_key""",
            (channel_id,),
        )).fetchall()
        return {str(row["item_key"])[len("template:"):] for row in rows}

    async def stage_channel_custom_template_reset(
        self, *, channel_id: int, template_key: str, updated_by: int
    ) -> None:
        from templates import TEMPLATE_REGISTRY
        spec = TEMPLATE_REGISTRY.get(template_key)
        if spec is None or spec.scope != "channel":
            raise ValueError("Template is not channel-scoped")
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        text = await self.get_channel_custom_template_text(
            channel_id=channel_id,
            template_key=template_key,
            include_legacy_template_overlay=False,
            revision_id=int(state["initial_revision_id"]),
        )
        if text is None:
            text = spec.default
        await self.set_channel_custom_draft_template_text(
            channel_id=channel_id, template_key=template_key,
            custom_text=text, updated_by=updated_by,
        )

    async def stage_all_channel_custom_template_resets(
        self, *, channel_id: int, updated_by: int
    ) -> int:
        from templates import TEMPLATE_REGISTRY
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        initial_items = await self.get_channel_custom_items(
            channel_id=channel_id,
            revision_id=int(state["initial_revision_id"]),
            include_legacy_template_overlay=False,
        )
        staged = 0
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_custom_draft_locked(channel_id=channel_id, updated_by=updated_by)
                for key, spec in sorted(TEMPLATE_REGISTRY.items()):
                    if spec.scope != "channel":
                        continue
                    item = initial_items.get(f"template:{key}")
                    text = None
                    if item is not None and isinstance(item.get("payload"), dict):
                        value = item["payload"].get("text")
                        if isinstance(value, str):
                            text = value
                    if text is None:
                        text = spec.default
                    await self._set_custom_draft_item_locked(
                        channel_id=channel_id,
                        item_key=f"template:{key}",
                        operation="set",
                        item_type=CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                        payload_json=_custom_text_payload(text=text, scope=spec.scope),
                        updated_by=updated_by,
                    )
                    staged += 1
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        return staged

    async def set_channel_custom_draft_start_card_media(
        self, *, channel_id: int, media_type: str, media_file_id: str, updated_by: int
    ) -> None:
        if media_type not in {"photo", "video", "animation"} or not media_file_id:
            raise ValueError("Unsupported start-card media")
        payload = json.dumps(
            {"media_type": media_type, "media_file_id": media_file_id},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self._set_custom_draft_item_locked(
                    channel_id=channel_id, item_key="start_card.media", operation="set",
                    item_type=CUSTOM_ITEM_TYPE_START_CARD_MEDIA, payload_json=payload,
                    updated_by=updated_by,
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def stage_channel_custom_start_card_media_removal(
        self, *, channel_id: int, updated_by: int
    ) -> None:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self._set_custom_draft_item_locked(
                    channel_id=channel_id, item_key="start_card.media", operation="delete",
                    item_type=None, payload_json=None, updated_by=updated_by,
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def get_channel_custom_draft_start_card_media(self, channel_id: int) -> dict[str, object] | None:
        row = await (await self.conn.execute(
            """SELECT operation,payload_json FROM channel_custom_draft_items
               WHERE channel_id=? AND item_key='start_card.media'""",
            (channel_id,),
        )).fetchone()
        if row is None:
            return None
        operation = str(row["operation"])
        if operation == "delete":
            return {"operation": "delete"}
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"operation": "invalid"}
        if not isinstance(payload, dict):
            return {"operation": "invalid"}
        return {
            "operation": "set",
            "media_type": payload.get("media_type"),
            "media_file_id": payload.get("media_file_id"),
        }

    async def get_channel_custom_start_card_media(
        self, channel_id: int, *, revision_id: int | None = None
    ) -> dict[str, str] | None:
        items = await self.get_channel_custom_items(
            channel_id=channel_id, revision_id=revision_id,
            include_legacy_template_overlay=False
        )
        item = items.get("start_card.media")
        if item is None or not isinstance(item.get("payload"), dict):
            return None
        payload = item["payload"]
        media_type = payload.get("media_type")
        media_file_id = payload.get("media_file_id")
        if media_type not in {"photo", "video", "animation"} or not isinstance(media_file_id, str) or not media_file_id:
            return None
        return {"media_type": str(media_type), "media_file_id": media_file_id}

    @staticmethod
    def _plan_supported_channel_custom_changes(
        *,
        current: dict[str, tuple[str, str]],
        target: dict[str, tuple[str, str]],
        fallback: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        """Build safe draft instructions for the owner-customizable surface only.

        Channel-scoped template text and the post-Start media card are the only
        bulk-copyable item types. Global templates are intentionally ignored;
        unknown/legacy changed items are reported as skipped instead of being
        rewritten blindly.
        """
        from templates import TEMPLATE_REGISTRY, validate_template

        fallback = fallback or {}
        instructions: list[tuple[str, str, str | None, str | None]] = []
        skipped_keys: list[str] = []
        for item_key in sorted(set(current) | set(target)):
            current_item = current.get(item_key)
            target_item = target.get(item_key)
            if current_item == target_item:
                continue

            if item_key == "start_card.media":
                if target_item is None:
                    if current_item is not None:
                        instructions.append((item_key, "delete", None, None))
                    continue
                item_type, raw_payload = target_item
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    skipped_keys.append(item_key)
                    continue
                media_type = payload.get("media_type") if isinstance(payload, dict) else None
                media_file_id = payload.get("media_file_id") if isinstance(payload, dict) else None
                if (
                    item_type == CUSTOM_ITEM_TYPE_START_CARD_MEDIA
                    and media_type in {"photo", "video", "animation"}
                    and isinstance(media_file_id, str) and media_file_id
                ):
                    instructions.append((item_key, "set", item_type, raw_payload))
                else:
                    skipped_keys.append(item_key)
                continue

            if not item_key.startswith("template:"):
                skipped_keys.append(item_key)
                continue
            template_key = item_key[len("template:"):]
            spec = TEMPLATE_REGISTRY.get(template_key)
            # Global bot/profile texts never belong to a Channel Custom Pack
            # operation even if old immutable snapshots contain them.
            if spec is not None and spec.scope != "channel":
                continue
            if spec is None:
                skipped_keys.append(item_key)
                continue

            candidate = target_item
            if candidate is None:
                fallback_item = fallback.get(item_key)
                candidate = fallback_item if fallback_item is not None else (
                    CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                    _custom_text_payload(text=spec.default, scope=spec.scope),
                )
            item_type, raw_payload = candidate
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                skipped_keys.append(item_key)
                continue
            text = payload.get("text") if isinstance(payload, dict) else None
            if item_type != CUSTOM_ITEM_TYPE_TEMPLATE_TEXT or not isinstance(text, str):
                skipped_keys.append(item_key)
                continue
            try:
                validate_template(template_key, text)
            except ValueError:
                skipped_keys.append(item_key)
                continue
            normalized_payload = _custom_text_payload(text=text, scope=spec.scope)
            normalized_target = (CUSTOM_ITEM_TYPE_TEMPLATE_TEXT, normalized_payload)
            if current_item != normalized_target:
                instructions.append((item_key, "set", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT, normalized_payload))

        return {
            "instructions": instructions,
            "changed_keys": [item[0] for item in instructions],
            "skipped_keys": skipped_keys,
            "staged": len(instructions),
            "skipped": len(skipped_keys),
        }

    async def _raw_channel_revision_items(self, *, channel_id: int, revision_id: int) -> dict[str, tuple[str, str]]:
        revision = await (await self.conn.execute(
            "SELECT 1 FROM channel_custom_revisions WHERE channel_id=? AND revision_id=?",
            (channel_id, revision_id),
        )).fetchone()
        if revision is None:
            raise ValueError("Customization revision is unavailable")
        rows = await (await self.conn.execute(
            "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=?",
            (revision_id,),
        )).fetchall()
        return {
            str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
            for row in rows
        }

    async def _raw_standard_revision_items(self, revision_id: int | None) -> dict[str, tuple[str, str]]:
        if revision_id is None:
            return {}
        revision = await (await self.conn.execute(
            "SELECT 1 FROM bot_standard_custom_revisions WHERE revision_id=?", (int(revision_id),)
        )).fetchone()
        if revision is None:
            raise ValueError("Standard Custom Pack revision is unavailable")
        rows = await (await self.conn.execute(
            "SELECT item_key,item_type,payload_json FROM bot_standard_custom_items WHERE revision_id=?",
            (int(revision_id),),
        )).fetchall()
        return {
            str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
            for row in rows
        }

    async def _plan_channel_custom_target(
        self,
        *,
        channel_id: int,
        target: dict[str, tuple[str, str]],
        fallback: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        current = await self._raw_channel_revision_items(
            channel_id=channel_id, revision_id=int(state["active_revision_id"])
        )
        plan = self._plan_supported_channel_custom_changes(
            current=current, target=target, fallback=fallback,
        )
        plan["base_revision_id"] = int(state["active_revision_id"])
        return plan

    async def plan_channel_custom_initial_reset(self, *, channel_id: int) -> dict[str, object]:
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        initial_revision_id = int(state["initial_revision_id"])
        revision = await self.get_channel_custom_revision(
            channel_id=channel_id, revision_id=initial_revision_id
        )
        if revision is None:
            raise ValueError("Initial customization revision is unavailable")
        target = await self._raw_channel_revision_items(
            channel_id=channel_id, revision_id=initial_revision_id
        )
        source_standard_revision_id = revision["source_standard_revision_id"]
        fallback = await self._raw_standard_revision_items(
            None if source_standard_revision_id is None else int(source_standard_revision_id)
        )
        result = await self._plan_channel_custom_target(
            channel_id=channel_id, target=target, fallback=fallback,
        )
        result.update({
            "target_revision_id": initial_revision_id,
            "source_standard_revision_id": None if source_standard_revision_id is None else int(source_standard_revision_id),
        })
        return result

    async def plan_channel_custom_apply_current_standard(self, *, channel_id: int) -> dict[str, object]:
        standard_state = await self.get_standard_custom_state()
        if standard_state is None:
            raise ValueError("Standard Custom Pack state is unavailable")
        standard_revision_id = int(standard_state["active_revision_id"])
        target = await self._raw_standard_revision_items(standard_revision_id)
        result = await self._plan_channel_custom_target(
            channel_id=channel_id, target=target, fallback=target,
        )
        result["source_standard_revision_id"] = standard_revision_id
        return result

    async def _validate_copy_source(self, *, channel_id: int, source_channel_id: int, actor_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        if channel_id == source_channel_id:
            raise ValueError("Source and target channels must differ")
        target = await (await self.conn.execute(
            "SELECT * FROM channels WHERE channel_id=? AND enabled=1", (channel_id,)
        )).fetchone()
        source = await (await self.conn.execute(
            "SELECT * FROM channels WHERE channel_id=? AND enabled=1", (source_channel_id,)
        )).fetchone()
        if target is None or source is None:
            raise PermissionError("Customization copy source is unavailable")
        if int(target["owner_id"]) != actor_id or int(source["owner_id"]) != actor_id:
            raise PermissionError("Customization copy requires the same channel owner")
        return target, source

    async def plan_channel_custom_copy(
        self, *, channel_id: int, source_channel_id: int, actor_id: int
    ) -> dict[str, object]:
        _, source = await self._validate_copy_source(
            channel_id=channel_id, source_channel_id=source_channel_id, actor_id=actor_id
        )
        source_state = await self.get_channel_custom_state(source_channel_id)
        if source_state is None:
            raise ValueError("Source customization state is unavailable")
        source_revision_id = int(source_state["active_revision_id"])
        source_revision = await self.get_channel_custom_revision(
            channel_id=source_channel_id, revision_id=source_revision_id
        )
        if source_revision is None:
            raise ValueError("Source customization revision is unavailable")
        target = await self._raw_channel_revision_items(
            channel_id=source_channel_id, revision_id=source_revision_id
        )
        source_standard_revision_id = source_revision["source_standard_revision_id"]
        fallback = await self._raw_standard_revision_items(
            None if source_standard_revision_id is None else int(source_standard_revision_id)
        )
        result = await self._plan_channel_custom_target(
            channel_id=channel_id, target=target, fallback=fallback,
        )
        result.update({
            "source_channel_id": int(source["channel_id"]),
            "source_channel_name": str(source["group_title"]),
            "source_revision_id": source_revision_id,
            "source_standard_revision_id": None if source_standard_revision_id is None else int(source_standard_revision_id),
        })
        return result

    async def _stage_bulk_custom_plan_locked(
        self,
        *,
        channel_id: int,
        actor_id: int,
        plan: dict[str, object],
        publish_source: str,
        publish_summary: str,
        audit_action: str,
        source_channel_id: int | None = None,
        source_standard_revision_id: int | None = None,
        audit_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        existing = await (await self.conn.execute(
            "SELECT 1 FROM channel_custom_drafts WHERE channel_id=?", (channel_id,)
        )).fetchone()
        if existing is not None:
            raise DraftNotEmptyError("Publish or discard the current draft first")
        instructions = list(plan.get("instructions") or [])
        if not instructions:
            raise ValueError("Customization source has no applicable differences")
        await self._ensure_custom_draft_locked(channel_id=channel_id, updated_by=actor_id)
        for item_key, operation, item_type, payload_json in instructions:
            await self._set_custom_draft_item_locked(
                channel_id=channel_id,
                item_key=str(item_key),
                operation=str(operation),
                item_type=None if item_type is None else str(item_type),
                payload_json=None if payload_json is None else str(payload_json),
                updated_by=actor_id,
            )
        now = dt_to_db(utc_now())
        await self.conn.execute(
            """UPDATE channel_custom_drafts
               SET publish_source=?,publish_summary=?,restore_revision_id=NULL,
                   source_channel_id=?,source_standard_revision_id=?,updated_at=?,updated_by=?
               WHERE channel_id=?""",
            (
                publish_source, publish_summary, source_channel_id, source_standard_revision_id,
                now, actor_id, channel_id,
            ),
        )
        metadata = {
            "base_revision_id": int(plan.get("base_revision_id") or 0),
            "staged_keys": list(plan.get("changed_keys") or []),
            "skipped_keys": list(plan.get("skipped_keys") or []),
            "source_channel_id": source_channel_id,
            "source_standard_revision_id": source_standard_revision_id,
        }
        if audit_metadata:
            metadata.update(audit_metadata)
        await self.conn.execute(
            """INSERT INTO customization_audit_log(
                   actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
               ) VALUES(?,'channel_custom',?,?,?,NULL,?,?)""",
            (
                actor_id, channel_id, channel_id, audit_action,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), now,
            ),
        )
        return {
            **plan,
            "publish_source": publish_source,
            "source_channel_id": source_channel_id,
            "source_standard_revision_id": source_standard_revision_id,
        }

    async def stage_channel_custom_initial_reset(
        self, *, channel_id: int, reset_by: int
    ) -> dict[str, object]:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                plan = await self.plan_channel_custom_initial_reset(channel_id=channel_id)
                result = await self._stage_bulk_custom_plan_locked(
                    channel_id=channel_id,
                    actor_id=reset_by,
                    plan=plan,
                    publish_source="reset_initial",
                    publish_summary="Restore the initial channel customization snapshot",
                    audit_action="initial_reset_staged",
                    source_standard_revision_id=plan.get("source_standard_revision_id"),
                    audit_metadata={"target_revision_id": plan.get("target_revision_id")},
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def stage_channel_custom_current_standard(
        self, *, channel_id: int, applied_by: int
    ) -> dict[str, object]:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                plan = await self.plan_channel_custom_apply_current_standard(channel_id=channel_id)
                standard_revision_id = int(plan["source_standard_revision_id"])
                result = await self._stage_bulk_custom_plan_locked(
                    channel_id=channel_id,
                    actor_id=applied_by,
                    plan=plan,
                    publish_source="apply_current_standard",
                    publish_summary=f"Apply Standard Custom Pack revision #{standard_revision_id}",
                    audit_action="current_standard_staged",
                    source_standard_revision_id=standard_revision_id,
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def stage_channel_custom_copy(
        self, *, channel_id: int, source_channel_id: int, copied_by: int
    ) -> dict[str, object]:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                plan = await self.plan_channel_custom_copy(
                    channel_id=channel_id, source_channel_id=source_channel_id, actor_id=copied_by
                )
                source_revision_id = int(plan["source_revision_id"])
                source_standard_revision_id = plan.get("source_standard_revision_id")
                result = await self._stage_bulk_custom_plan_locked(
                    channel_id=channel_id,
                    actor_id=copied_by,
                    plan=plan,
                    publish_source="copy_from_channel",
                    publish_summary=f"Copy published customization from channel #{source_channel_id} revision #{source_revision_id}",
                    audit_action="channel_copy_staged",
                    source_channel_id=source_channel_id,
                    source_standard_revision_id=source_standard_revision_id,
                    audit_metadata={"source_revision_id": source_revision_id},
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def export_channel_custom_pack(
        self, *, channel_id: int, exported_by: int
    ) -> dict[str, object]:
        """Build and audit a safe export of one owner's published customization.

        Only presentation data from the active immutable channel revision is
        exported.  Subscriber/moderation/security tables are never queried.
        """
        from custom_transfer import build_export_document, dumps_export_document
        from templates import TEMPLATE_REGISTRY

        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                channel = await (await self.conn.execute(
                    "SELECT channel_id,owner_id,group_title,enabled FROM channels WHERE channel_id=?",
                    (channel_id,),
                )).fetchone()
                if channel is None or not bool(channel["enabled"]) or int(channel["owner_id"]) != exported_by:
                    raise PermissionError("Customization export requires the channel owner")
                state = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_state WHERE channel_id=?", (channel_id,)
                )).fetchone()
                if state is None:
                    raise ValueError("Channel customization state is unavailable")
                revision_id = int(state["active_revision_id"])
                revision = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_revisions WHERE channel_id=? AND revision_id=?",
                    (channel_id, revision_id),
                )).fetchone()
                if revision is None:
                    raise ValueError("Active customization revision is unavailable")
                rows = await (await self.conn.execute(
                    "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=? ORDER BY item_key",
                    (revision_id,),
                )).fetchall()
                raw_items = {
                    str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
                    for row in rows
                }

                template_texts: dict[str, str] = {}
                supported_item_keys = {"start_card.media"}
                for key, spec in sorted(TEMPLATE_REGISTRY.items()):
                    if spec.scope != "channel":
                        continue
                    item_key = f"template:{key}"
                    supported_item_keys.add(item_key)
                    raw_item = raw_items.get(item_key)
                    text = None
                    if raw_item is not None and raw_item[0] == CUSTOM_ITEM_TYPE_TEMPLATE_TEXT:
                        try:
                            payload = json.loads(raw_item[1])
                        except (TypeError, ValueError, json.JSONDecodeError):
                            payload = None
                        candidate = payload.get("text") if isinstance(payload, dict) else None
                        if isinstance(candidate, str):
                            text = candidate
                    template_texts[key] = spec.default if text is None else text

                media = None
                raw_media = raw_items.get("start_card.media")
                if raw_media is not None and raw_media[0] == CUSTOM_ITEM_TYPE_START_CARD_MEDIA:
                    try:
                        parsed_media = json.loads(raw_media[1])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_media = None
                    if isinstance(parsed_media, dict):
                        media_type = parsed_media.get("media_type")
                        media_file_id = parsed_media.get("media_file_id")
                        if (
                            media_type in {"photo", "video", "animation"}
                            and isinstance(media_file_id, str) and media_file_id
                        ):
                            media = {
                                "media_type": str(media_type),
                                "media_file_id": media_file_id,
                            }

                omitted = sum(1 for key in raw_items if key not in supported_item_keys)
                source_standard = revision["source_standard_revision_id"]
                document = build_export_document(
                    channel_id=channel_id,
                    channel_title=str(channel["group_title"]),
                    revision_id=revision_id,
                    source_standard_revision_id=(
                        None if source_standard is None else int(source_standard)
                    ),
                    template_texts=template_texts,
                    media=media,
                    omitted_unsupported_items=omitted,
                )
                digest = hashlib.sha256(dumps_export_document(document)).hexdigest()
                now = dt_to_db(utc_now())
                await self.conn.execute(
                    """INSERT INTO customization_audit_log(
                           actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                       ) VALUES(?,'channel_custom',?,?, 'custom_exported',NULL,?,?)""",
                    (
                        exported_by, channel_id, channel_id,
                        json.dumps({
                            "revision_id": revision_id,
                            "schema_version": int(document["schema_version"]),
                            "document_sha256": digest,
                            "omitted_unsupported_items": omitted,
                        }, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                await self.conn.commit()
                return document
            except Exception:
                await self.conn.rollback()
                raise

    async def plan_channel_custom_import(
        self, *, channel_id: int, actor_id: int, pack
    ) -> dict[str, object]:
        """Permission-check and diff a normalized transfer pack against live state."""
        from custom_transfer import NormalizedCustomPack
        from templates import TEMPLATE_REGISTRY

        if not isinstance(pack, NormalizedCustomPack):
            raise TypeError("NormalizedCustomPack is required")
        channel = await (await self.conn.execute(
            "SELECT channel_id,owner_id,enabled FROM channels WHERE channel_id=?", (channel_id,)
        )).fetchone()
        if channel is None or not bool(channel["enabled"]) or int(channel["owner_id"]) != actor_id:
            raise PermissionError("Customization import requires the channel owner")
        state = await self.get_channel_custom_state(channel_id)
        if state is None:
            raise ValueError("Channel customization state is unavailable")
        current = await self._raw_channel_revision_items(
            channel_id=channel_id, revision_id=int(state["active_revision_id"])
        )
        target = dict(current)
        for template_key, text in sorted(pack.templates.items()):
            spec = TEMPLATE_REGISTRY.get(template_key)
            if spec is None or spec.scope != "channel":
                raise ValueError("Imported template is not channel-scoped")
            target[f"template:{template_key}"] = (
                CUSTOM_ITEM_TYPE_TEMPLATE_TEXT,
                _custom_text_payload(text=text, scope=spec.scope),
            )
        if pack.media_type is None or pack.media_file_id is None:
            target.pop("start_card.media", None)
        else:
            target["start_card.media"] = (
                CUSTOM_ITEM_TYPE_START_CARD_MEDIA,
                json.dumps(
                    {"media_type": pack.media_type, "media_file_id": pack.media_file_id},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
            )
        plan = self._plan_supported_channel_custom_changes(current=current, target=target)
        plan.update({
            "base_revision_id": int(state["active_revision_id"]),
            "import_schema_version": 1,
            "import_source_channel_id": pack.source_channel_id,
            "import_source_channel_title": pack.source_channel_title,
            "import_source_revision_id": pack.source_revision_id,
            "import_source_standard_revision_id": pack.source_standard_revision_id,
            "import_document_sha256": pack.document_sha256,
            "import_has_media": pack.media_file_id is not None,
        })
        return plan

    async def stage_channel_custom_import(
        self, *, channel_id: int, imported_by: int, pack
    ) -> dict[str, object]:
        """Stage a validated transfer pack into a fresh draft, never directly live."""
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-run permissions and diff at the moment of staging. This
                # prevents stale FSM/callback data from bypassing ownership.
                plan = await self.plan_channel_custom_import(
                    channel_id=channel_id, actor_id=imported_by, pack=pack
                )
                source_title = str(plan.get("import_source_channel_title") or "")
                source_revision_id = plan.get("import_source_revision_id")
                summary = "Import Channel Custom Pack schema v1"
                if source_title:
                    summary += f" from {source_title}"
                if source_revision_id is not None:
                    summary += f" revision #{int(source_revision_id)}"
                result = await self._stage_bulk_custom_plan_locked(
                    channel_id=channel_id,
                    actor_id=imported_by,
                    plan=plan,
                    publish_source="import",
                    publish_summary=summary,
                    audit_action="custom_imported",
                    audit_metadata={
                        "status": "staged",
                        "schema_version": 1,
                        "source_channel_id": plan.get("import_source_channel_id"),
                        "source_channel_title": source_title,
                        "source_revision_id": source_revision_id,
                        "source_standard_revision_id": plan.get("import_source_standard_revision_id"),
                        "document_sha256": plan.get("import_document_sha256"),
                        "has_media": bool(plan.get("import_has_media")),
                    },
                )
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    async def stage_channel_custom_revision_restore(
        self, *, channel_id: int, revision_id: int, restored_by: int
    ) -> dict[str, object]:
        """Restore a historical revision into a fresh draft, never directly to live.

        Only supported channel customization item types are staged. Historical
        unknown/legacy items are left untouched rather than risking data loss.
        Publication later creates a new immutable revision with source
        ``rollback``.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                state = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_state WHERE channel_id=?", (channel_id,)
                )).fetchone()
                target_revision = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_revisions WHERE channel_id=? AND revision_id=?",
                    (channel_id, revision_id),
                )).fetchone()
                if state is None or target_revision is None:
                    raise ValueError("Customization revision is unavailable")
                active_revision_id = int(state["active_revision_id"])
                if active_revision_id == revision_id:
                    raise ValueError("Customization revision is already active")

                existing_draft = await (await self.conn.execute(
                    "SELECT 1 FROM channel_custom_draft_items WHERE channel_id=? LIMIT 1",
                    (channel_id,),
                )).fetchone()
                if existing_draft is not None:
                    raise DraftNotEmptyError("Publish or discard the current draft first")

                current_rows = await (await self.conn.execute(
                    "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=?",
                    (active_revision_id,),
                )).fetchall()
                target_rows = await (await self.conn.execute(
                    "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=?",
                    (revision_id,),
                )).fetchall()
                current = {
                    str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
                    for row in current_rows
                }
                target = {
                    str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
                    for row in target_rows
                }
                target_standard: dict[str, tuple[str, str]] = {}
                source_standard_revision_id = target_revision["source_standard_revision_id"]
                if source_standard_revision_id is not None:
                    standard_rows = await (await self.conn.execute(
                        "SELECT item_key,item_type,payload_json FROM bot_standard_custom_items WHERE revision_id=?",
                        (int(source_standard_revision_id),),
                    )).fetchall()
                    target_standard = {
                        str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
                        for row in standard_rows
                    }

                from templates import TEMPLATE_REGISTRY, validate_template
                instructions: list[tuple[str, str, str | None, str | None]] = []
                skipped_keys: list[str] = []
                for item_key in sorted(set(current) | set(target)):
                    if current.get(item_key) == target.get(item_key):
                        continue
                    target_item = target.get(item_key)
                    if target_item is None:
                        current_item = current.get(item_key)
                        if item_key == "start_card.media" and current_item is not None:
                            instructions.append((item_key, "delete", None, None))
                            continue
                        if current_item is not None and current_item[0] == CUSTOM_ITEM_TYPE_TEMPLATE_TEXT and item_key.startswith("template:"):
                            template_key = item_key[len("template:"):]
                            spec = TEMPLATE_REGISTRY.get(template_key)
                            if spec is not None and spec.scope == "channel":
                                standard_item = target_standard.get(item_key)
                                payload = None
                                if standard_item is not None and standard_item[0] == CUSTOM_ITEM_TYPE_TEMPLATE_TEXT:
                                    try:
                                        parsed_standard = json.loads(standard_item[1])
                                    except (TypeError, ValueError, json.JSONDecodeError):
                                        parsed_standard = None
                                    standard_text = parsed_standard.get("text") if isinstance(parsed_standard, dict) else None
                                    if isinstance(standard_text, str):
                                        try:
                                            validate_template(template_key, standard_text)
                                        except ValueError:
                                            standard_text = None
                                    if isinstance(standard_text, str):
                                        payload = _custom_text_payload(text=standard_text, scope=spec.scope)
                                if payload is None:
                                    payload = _custom_text_payload(text=spec.default, scope=spec.scope)
                                if current_item != (CUSTOM_ITEM_TYPE_TEMPLATE_TEXT, payload):
                                    instructions.append((item_key, "set", CUSTOM_ITEM_TYPE_TEMPLATE_TEXT, payload))
                                continue
                        skipped_keys.append(item_key)
                        continue

                    item_type, raw_payload = target_item
                    try:
                        payload = json.loads(raw_payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        skipped_keys.append(item_key)
                        continue
                    if item_type == CUSTOM_ITEM_TYPE_TEMPLATE_TEXT and item_key.startswith("template:"):
                        template_key = item_key[len("template:"):]
                        spec = TEMPLATE_REGISTRY.get(template_key)
                        text = payload.get("text") if isinstance(payload, dict) else None
                        if spec is None or spec.scope != "channel" or not isinstance(text, str):
                            skipped_keys.append(item_key)
                            continue
                        try:
                            validate_template(template_key, text)
                        except ValueError:
                            skipped_keys.append(item_key)
                            continue
                        instructions.append((item_key, "set", item_type, raw_payload))
                        continue
                    if item_type == CUSTOM_ITEM_TYPE_START_CARD_MEDIA and item_key == "start_card.media":
                        media_type = payload.get("media_type") if isinstance(payload, dict) else None
                        media_file_id = payload.get("media_file_id") if isinstance(payload, dict) else None
                        if media_type in {"photo", "video", "animation"} and isinstance(media_file_id, str) and media_file_id:
                            instructions.append((item_key, "set", item_type, raw_payload))
                        else:
                            skipped_keys.append(item_key)
                        continue
                    skipped_keys.append(item_key)

                if not instructions:
                    raise ValueError("Revision has no restorable differences")

                await self._ensure_custom_draft_locked(channel_id=channel_id, updated_by=restored_by)
                for item_key, operation, item_type, payload_json in instructions:
                    await self._set_custom_draft_item_locked(
                        channel_id=channel_id, item_key=item_key, operation=operation,
                        item_type=item_type, payload_json=payload_json, updated_by=restored_by,
                    )
                await self.conn.execute(
                    """UPDATE channel_custom_drafts
                       SET publish_source='rollback',publish_summary=?,restore_revision_id=?,
                           source_channel_id=NULL,source_standard_revision_id=?,updated_at=?,updated_by=?
                       WHERE channel_id=?""",
                    (
                        f"Restore revision #{revision_id} as a new revision",
                        revision_id,
                        None if source_standard_revision_id is None else int(source_standard_revision_id),
                        dt_to_db(utc_now()), restored_by, channel_id,
                    ),
                )
                now = dt_to_db(utc_now())
                await self.conn.execute(
                    """INSERT INTO customization_audit_log(
                           actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                       ) VALUES(?,'channel_custom',?,?, 'revision_restore_staged',NULL,?,?)""",
                    (
                        restored_by, channel_id, channel_id,
                        json.dumps({
                            "active_revision_id": active_revision_id,
                            "target_revision_id": revision_id,
                            "staged_keys": [item[0] for item in instructions],
                            "skipped_keys": skipped_keys,
                        }, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                await self.conn.commit()
                return {
                    "target_revision_id": revision_id,
                    "base_revision_id": active_revision_id,
                    "staged": len(instructions),
                    "skipped": len(skipped_keys),
                }
            except Exception:
                await self.conn.rollback()
                raise

    async def discard_channel_custom_draft(self, *, channel_id: int, discarded_by: int) -> bool:
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                count = await self.get_channel_custom_draft_count(channel_id)
                cursor = await self.conn.execute(
                    "DELETE FROM channel_custom_drafts WHERE channel_id=?", (channel_id,)
                )
                if cursor.rowcount > 0:
                    now = dt_to_db(utc_now())
                    await self.conn.execute(
                        """INSERT INTO customization_audit_log(
                               actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                           ) VALUES(?,'channel_custom',?,?, 'draft_discarded',NULL,?,?)""",
                        (discarded_by, channel_id, channel_id, json.dumps({"items": count}, sort_keys=True), now),
                    )
                await self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                await self.conn.rollback()
                raise

    async def publish_channel_custom_draft(
        self, *, channel_id: int, published_by: int, summary: str | None = None
    ) -> int:
        """Atomically publish all draft items as one new immutable revision."""
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                state = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_state WHERE channel_id=?", (channel_id,)
                )).fetchone()
                draft = await (await self.conn.execute(
                    "SELECT * FROM channel_custom_drafts WHERE channel_id=?", (channel_id,)
                )).fetchone()
                if state is None or draft is None:
                    raise ValueError("Customization draft is empty")
                active_revision_id = int(state["active_revision_id"])
                base_revision_id = int(draft["base_revision_id"])
                if active_revision_id != base_revision_id:
                    raise DraftConflictError("Draft base revision is stale")
                draft_rows = await (await self.conn.execute(
                    """SELECT item_key,operation,item_type,payload_json
                       FROM channel_custom_draft_items WHERE channel_id=? ORDER BY item_key""",
                    (channel_id,),
                )).fetchall()
                if not draft_rows:
                    raise ValueError("Customization draft is empty")

                base_rows = await (await self.conn.execute(
                    "SELECT item_key,item_type,payload_json FROM channel_custom_items WHERE revision_id=? ORDER BY item_key",
                    (active_revision_id,),
                )).fetchall()
                items = {
                    str(row["item_key"]): (str(row["item_type"]), str(row["payload_json"]))
                    for row in base_rows
                }
                changed_keys: list[str] = []
                from templates import TEMPLATE_REGISTRY, validate_template
                for row in draft_rows:
                    item_key = str(row["item_key"])
                    operation = str(row["operation"])
                    if operation == "delete":
                        if item_key != "start_card.media":
                            raise ValueError("Unsupported draft delete operation")
                        items.pop(item_key, None)
                    else:
                        item_type = str(row["item_type"])
                        raw_payload = str(row["payload_json"])
                        try:
                            parsed = json.loads(raw_payload)
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise ValueError("Invalid draft payload") from exc
                        if item_type == CUSTOM_ITEM_TYPE_TEMPLATE_TEXT and item_key.startswith("template:"):
                            template_key = item_key[len("template:"):]
                            spec = TEMPLATE_REGISTRY.get(template_key)
                            text = parsed.get("text") if isinstance(parsed, dict) else None
                            if spec is None or spec.scope != "channel" or not isinstance(text, str):
                                raise ValueError("Invalid draft template item")
                            validate_template(template_key, text)
                        elif item_type == CUSTOM_ITEM_TYPE_START_CARD_MEDIA and item_key == "start_card.media":
                            media_type = parsed.get("media_type") if isinstance(parsed, dict) else None
                            media_file_id = parsed.get("media_file_id") if isinstance(parsed, dict) else None
                            if media_type not in {"photo", "video", "animation"} or not isinstance(media_file_id, str) or not media_file_id:
                                raise ValueError("Invalid draft start-card media")
                        else:
                            raise ValueError("Unsupported draft item type")
                        items[item_key] = (item_type, raw_payload)
                    changed_keys.append(item_key)

                publish_source = str(draft["publish_source"] or "manual_publish")
                if publish_source not in {
                    "manual_publish", "rollback", "reset_initial",
                    "apply_current_standard", "copy_from_channel", "import",
                }:
                    raise ValueError("Unsupported draft publication source")
                draft_summary = draft["publish_summary"]
                effective_summary = summary or (str(draft_summary) if draft_summary else None)
                if effective_summary is None:
                    effective_summary = f"Publish {len(changed_keys)} draft item(s)"

                restore_revision_id = draft["restore_revision_id"]
                source_channel_id = draft["source_channel_id"]
                draft_standard_revision_id = draft["source_standard_revision_id"]
                effective_standard_revision_id = (
                    int(draft_standard_revision_id)
                    if draft_standard_revision_id is not None
                    else (
                        None if state["source_standard_revision_id"] is None
                        else int(state["source_standard_revision_id"])
                    )
                )
                if effective_standard_revision_id is not None:
                    standard_exists = await (await self.conn.execute(
                        "SELECT 1 FROM bot_standard_custom_revisions WHERE revision_id=?",
                        (effective_standard_revision_id,),
                    )).fetchone()
                    if standard_exists is None:
                        raise ValueError("Draft Standard Custom Pack source is unavailable")

                if publish_source == "rollback":
                    if restore_revision_id is None:
                        raise ValueError("Rollback draft target is missing")
                    target = await (await self.conn.execute(
                        "SELECT 1 FROM channel_custom_revisions WHERE channel_id=? AND revision_id=?",
                        (channel_id, int(restore_revision_id)),
                    )).fetchone()
                    if target is None:
                        raise ValueError("Rollback draft target is unavailable")
                elif restore_revision_id is not None:
                    raise ValueError("Unexpected rollback target on non-rollback draft")

                if publish_source == "copy_from_channel":
                    if source_channel_id is None:
                        raise ValueError("Copy draft source channel is missing")
                    target_channel = await (await self.conn.execute(
                        "SELECT owner_id FROM channels WHERE channel_id=? AND enabled=1", (channel_id,)
                    )).fetchone()
                    source_channel = await (await self.conn.execute(
                        "SELECT owner_id FROM channels WHERE channel_id=? AND enabled=1", (int(source_channel_id),)
                    )).fetchone()
                    if (
                        target_channel is None or source_channel is None
                        or int(target_channel["owner_id"]) != published_by
                        or int(source_channel["owner_id"]) != published_by
                    ):
                        raise PermissionError("Copy draft no longer belongs to the same owner")
                elif source_channel_id is not None:
                    raise ValueError("Unexpected source channel on non-copy draft")

                if publish_source == "apply_current_standard" and effective_standard_revision_id is None:
                    raise ValueError("Standard source is missing")

                now = dt_to_db(utc_now())
                cursor = await self.conn.execute(
                    """INSERT INTO channel_custom_revisions(
                           channel_id,source,source_standard_revision_id,created_at,created_by,summary
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        channel_id, publish_source, effective_standard_revision_id,
                        now, published_by, effective_summary,
                    ),
                )
                revision_id = int(cursor.lastrowid)
                if items:
                    await self.conn.executemany(
                        "INSERT INTO channel_custom_items(revision_id,item_key,item_type,payload_json) VALUES(?,?,?,?)",
                        [(revision_id, key, item_type, payload) for key, (item_type, payload) in sorted(items.items())],
                    )
                await self.conn.execute(
                    """UPDATE channel_custom_state
                       SET active_revision_id=?,source_standard_revision_id=?,updated_at=?,updated_by=?
                       WHERE channel_id=?""",
                    (revision_id, effective_standard_revision_id, now, published_by, channel_id),
                )

                # Keep old storage as a compatibility mirror, but no owner edit
                # writes directly to it after schema v26.
                await self.conn.execute(
                    "DELETE FROM channel_template_overrides WHERE channel_id=?", (channel_id,)
                )
                media = items.get("start_card.media")
                if media is None:
                    await self.conn.execute(
                        "DELETE FROM channel_start_card_media WHERE channel_id=?", (channel_id,)
                    )
                else:
                    _, raw_payload = media
                    payload = json.loads(raw_payload)
                    await self.conn.execute(
                        """INSERT INTO channel_start_card_media(
                               channel_id,media_type,media_file_id,updated_at,updated_by
                           ) VALUES(?,?,?,?,?)
                           ON CONFLICT(channel_id) DO UPDATE SET
                               media_type=excluded.media_type,
                               media_file_id=excluded.media_file_id,
                               updated_at=excluded.updated_at,
                               updated_by=excluded.updated_by""",
                        (
                            channel_id, str(payload["media_type"]), str(payload["media_file_id"]),
                            now, published_by,
                        ),
                    )

                await self.conn.execute(
                    """INSERT INTO customization_audit_log(
                           actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                       ) VALUES(?,'channel_custom',?,?, 'draft_published',NULL,?,?)""",
                    (
                        published_by, channel_id, channel_id,
                        json.dumps({
                            "base_revision_id": base_revision_id,
                            "revision_id": revision_id,
                            "changed_keys": sorted(set(changed_keys)),
                            "source": publish_source,
                            "restore_revision_id": None if restore_revision_id is None else int(restore_revision_id),
                            "source_channel_id": None if source_channel_id is None else int(source_channel_id),
                            "source_standard_revision_id": effective_standard_revision_id,
                        }, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                await self.conn.execute(
                    "DELETE FROM channel_custom_drafts WHERE channel_id=?", (channel_id,)
                )
                await self.conn.commit()
                return revision_id
            except Exception:
                await self.conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Global pre-Start bot card. The Telegram description is bot-wide, so the
    # persisted draft is intentionally not keyed by channel_id.
    # ------------------------------------------------------------------
    async def get_channel_start_card_media(self, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_start_card_media WHERE channel_id=?",
            (channel_id,),
        )).fetchone()

    async def set_channel_start_card_media(
        self, *, channel_id: int, media_type: str, media_file_id: str, updated_by: int
    ) -> None:
        if media_type not in {"photo", "video", "animation"}:
            raise ValueError("Unsupported start-card media type")
        file_id = media_file_id.strip() if isinstance(media_file_id, str) else ""
        if not file_id or len(file_id) > 2048:
            raise ValueError("Invalid start-card media file id")
        now = dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO channel_start_card_media(
                       channel_id,media_type,media_file_id,updated_at,updated_by
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET
                       media_type=excluded.media_type,
                       media_file_id=excluded.media_file_id,
                       updated_at=excluded.updated_at,
                       updated_by=excluded.updated_by""",
                (channel_id, media_type, file_id, now, updated_by),
            )
            await self.conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(?,'channel_custom',?,?, 'channel_start_card_media_set','start_card.media',?,?)""",
                (updated_by, channel_id, channel_id, json.dumps({"media_type": media_type}, sort_keys=True), now),
            )
            await self.conn.commit()

    async def remove_channel_start_card_media(self, *, channel_id: int, updated_by: int) -> bool:
        now = dt_to_db(utc_now())
        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM channel_start_card_media WHERE channel_id=?",
                (channel_id,),
            )
            removed = bool(cursor.rowcount)
            if removed:
                await self.conn.execute(
                    """INSERT INTO customization_audit_log(
                           actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                       ) VALUES(?,'channel_custom',?,?, 'channel_start_card_media_removed','start_card.media',NULL,?)""",
                    (updated_by, channel_id, channel_id, now),
                )
            await self.conn.commit()
            return removed

    async def get_bot_prestart_card(self) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM bot_prestart_card WHERE singleton_id=1"
        )).fetchone()

    async def set_bot_prestart_description(self, *, description: str | None, updated_by: int) -> None:
        now = dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO bot_prestart_card(singleton_id,description_override,media_type,media_file_id,updated_at,updated_by)
                   VALUES(1,?,NULL,NULL,?,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                     description_override=excluded.description_override,
                     updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (description, now, updated_by),
            )
            await self.conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(?,'global_profile',1,NULL,'global_prestart_description_set','prestart.description',?,?)""",
                (updated_by, json.dumps({"length": len(description or "")}, sort_keys=True), now),
            )
            await self.conn.commit()

    async def set_bot_prestart_media(self, *, media_type: str, media_file_id: str, updated_by: int) -> None:
        if media_type not in {"photo", "video", "animation"} or not media_file_id:
            raise ValueError("Unsupported pre-start media")
        now = dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO bot_prestart_card(singleton_id,description_override,media_type,media_file_id,updated_at,updated_by)
                   VALUES(1,NULL,?,?,?,?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                     media_type=excluded.media_type, media_file_id=excluded.media_file_id,
                     updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (media_type, media_file_id, now, updated_by),
            )
            await self.conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(?,'global_profile',1,NULL,'global_prestart_media_set','prestart.media',?,?)""",
                (updated_by, json.dumps({"media_type": media_type}, sort_keys=True), now),
            )
            await self.conn.commit()

    async def remove_bot_prestart_media(self, *, updated_by: int) -> None:
        now = dt_to_db(utc_now())
        async with self._write_lock:
            row = await (await self.conn.execute(
                "SELECT description_override,media_type FROM bot_prestart_card WHERE singleton_id=1"
            )).fetchone()
            if row is None or row["media_type"] is None:
                return
            if row["description_override"] is None:
                await self.conn.execute("DELETE FROM bot_prestart_card WHERE singleton_id=1")
            else:
                await self.conn.execute(
                    "UPDATE bot_prestart_card SET media_type=NULL,media_file_id=NULL,updated_at=?,updated_by=? WHERE singleton_id=1",
                    (now, updated_by),
                )
            await self.conn.execute(
                """INSERT INTO customization_audit_log(
                       actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                   ) VALUES(?,'global_profile',1,NULL,'global_prestart_media_removed','prestart.media',NULL,?)""",
                (updated_by, now),
            )
            await self.conn.commit()

    async def reset_bot_prestart_card(self, *, updated_by: int | None = None) -> None:
        now = dt_to_db(utc_now())
        async with self._write_lock:
            row = await (await self.conn.execute(
                "SELECT 1 FROM bot_prestart_card WHERE singleton_id=1"
            )).fetchone()
            await self.conn.execute("DELETE FROM bot_prestart_card WHERE singleton_id=1")
            if row is not None:
                await self.conn.execute(
                    """INSERT INTO customization_audit_log(
                           actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
                       ) VALUES(?,'global_profile',1,NULL,'global_prestart_reset',NULL,NULL,?)""",
                    (updated_by, now),
                )
            await self.conn.commit()

    # ------------------------------------------------------------------
    # Channel mass broadcasts.  The delivery journal is keyed by real user,
    # never by topic, so anonymous/identified topic pairs cannot duplicate a
    # publication.  No identity from this layer is exposed to Telegram UI.
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_broadcast_source_ids(source_message_id: int, source_message_ids: Sequence[int] | None) -> tuple[int, ...]:
        values = tuple(int(value) for value in (source_message_ids or (source_message_id,)))
        if not values or len(values) > 100 or any(value <= 0 for value in values):
            raise ValueError("Invalid broadcast source message ids")
        ordered = tuple(sorted(set(values)))
        if len(ordered) != len(values):
            raise ValueError("Broadcast source message ids must be unique")
        return ordered

    async def create_broadcast_draft(
        self, *, channel_id: int, created_by: int, source_chat_id: int, source_message_id: int,
        source_message_ids: Sequence[int] | None = None, source_media_group_id: str | None = None,
    ) -> sqlite3.Row:
        broadcast_id = uuid.uuid4().hex
        now = dt_to_db(utc_now())
        source_ids = self._normalize_broadcast_source_ids(source_message_id, source_message_ids)
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO channel_broadcasts(broadcast_id,channel_id,created_by,source_chat_id,source_message_id,source_message_ids,source_media_group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'draft',?,?)",
                (broadcast_id, channel_id, created_by, source_chat_id, source_ids[0], json.dumps(source_ids), source_media_group_id, now, now),
            )
            await self.conn.commit()
        row = await self.get_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
        if row is None:
            raise RuntimeError("Broadcast draft was not created")
        return row

    async def update_broadcast_draft_source(
        self, *, broadcast_id: str, channel_id: int, created_by: int, source_chat_id: int, source_message_id: int,
        source_message_ids: Sequence[int] | None = None, source_media_group_id: str | None = None,
    ) -> bool:
        source_ids = self._normalize_broadcast_source_ids(source_message_id, source_message_ids)
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_broadcasts SET source_chat_id=?,source_message_id=?,source_message_ids=?,source_media_group_id=?,updated_at=? WHERE broadcast_id=? AND channel_id=? AND created_by=? AND status='draft'",
                (source_chat_id, source_ids[0], json.dumps(source_ids), source_media_group_id, dt_to_db(utc_now()), broadcast_id, channel_id, created_by),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    @staticmethod
    def broadcast_source_message_ids(broadcast: sqlite3.Row) -> tuple[int, ...]:
        raw = broadcast["source_message_ids"] if "source_message_ids" in broadcast.keys() else None
        if raw:
            try:
                values = tuple(int(value) for value in json.loads(str(raw)))
                if values and values == tuple(sorted(set(values))):
                    return values
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Invalid stored source_message_ids for broadcast %s", broadcast["broadcast_id"])
        return (int(broadcast["source_message_id"]),)

    async def get_broadcast(self, *, broadcast_id: str, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_broadcasts WHERE broadcast_id=? AND channel_id=?",
            (broadcast_id, channel_id),
        )).fetchone()

    async def get_sending_broadcast(self, *, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_broadcasts WHERE channel_id=? AND status='sending' ORDER BY started_at DESC LIMIT 1",
            (channel_id,),
        )).fetchone()

    async def cancel_broadcast_draft(self, *, broadcast_id: str, channel_id: int, created_by: int) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_broadcasts SET status='cancelled',updated_at=? WHERE broadcast_id=? AND channel_id=? AND created_by=? AND status='draft'",
                (dt_to_db(utc_now()), broadcast_id, channel_id, created_by),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def claim_broadcast_for_send(self, *, broadcast_id: str, channel_id: int, created_by: int) -> bool:
        """Atomically claim a draft and snapshot unique recipient user IDs."""
        now = dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (await self.conn.execute(
                    "SELECT status,created_by FROM channel_broadcasts WHERE broadcast_id=? AND channel_id=?",
                    (broadcast_id, channel_id),
                )).fetchone()
                if row is None or str(row["status"]) != "draft" or int(row["created_by"]) != created_by:
                    await self.conn.rollback()
                    return False
                other = await (await self.conn.execute(
                    "SELECT 1 FROM channel_broadcasts WHERE channel_id=? AND status='sending' AND broadcast_id!=?",
                    (channel_id, broadcast_id),
                )).fetchone()
                if other is not None:
                    await self.conn.rollback()
                    return False

                recipients = await (await self.conn.execute(
                    """SELECT DISTINCT s.user_id
                       FROM channel_subscribers s
                       WHERE s.channel_id=? AND (
                           EXISTS (SELECT 1 FROM channel_topics t WHERE t.channel_id=s.channel_id AND t.user_id=s.user_id)
                           OR EXISTS (SELECT 1 FROM message_events e WHERE e.channel_id=s.channel_id AND e.user_id=s.user_id)
                       )
                       ORDER BY s.user_id""",
                    (channel_id,),
                )).fetchall()
                await self.conn.execute(
                    "UPDATE channel_broadcasts SET status='sending',recipient_count=?,started_at=?,updated_at=? WHERE broadcast_id=? AND channel_id=?",
                    (len(recipients), now, now, broadcast_id, channel_id),
                )
                if recipients:
                    await self.conn.executemany(
                        "INSERT INTO channel_broadcast_deliveries(broadcast_id,channel_id,user_id,status) VALUES(?,?,?,'pending')",
                        [(broadcast_id, channel_id, int(item["user_id"])) for item in recipients],
                    )
                await self.conn.commit()
                return True
            except Exception:
                await self.conn.rollback()
                raise

    async def list_pending_broadcast_deliveries(self, *, broadcast_id: str, channel_id: int) -> list[sqlite3.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM channel_broadcast_deliveries WHERE broadcast_id=? AND channel_id=? AND status='pending' ORDER BY user_id",
            (broadcast_id, channel_id),
        )).fetchall()

    async def reserve_broadcast_delivery(self, *, broadcast_id: str, channel_id: int, user_id: int) -> dict[str, object] | None:
        """Reserve one recipient and resolve only their current privacy/topic route."""
        async with self._write_lock:
            pending = await (await self.conn.execute(
                "SELECT 1 FROM channel_broadcast_deliveries WHERE broadcast_id=? AND channel_id=? AND user_id=? AND status='pending'",
                (broadcast_id, channel_id, user_id),
            )).fetchone()
            if pending is None:
                return None
            route = await (await self.conn.execute(
                """SELECT COALESCE(p.privacy_mode,'identified') AS privacy_mode,
                          t.group_id, t.topic_id, t.status AS topic_status
                   FROM channel_subscribers s
                   LEFT JOIN channel_subscriber_privacy p ON p.channel_id=s.channel_id AND p.user_id=s.user_id
                   LEFT JOIN channel_topics t ON t.channel_id=s.channel_id AND t.user_id=s.user_id
                        AND t.privacy_mode=COALESCE(p.privacy_mode,'identified')
                   WHERE s.channel_id=? AND s.user_id=?""",
                (channel_id, user_id),
            )).fetchone()
            privacy_mode = str(route["privacy_mode"]) if route is not None else "identified"
            topic_id = int(route["topic_id"]) if route is not None and route["topic_id"] is not None else None
            cursor = await self.conn.execute(
                "UPDATE channel_broadcast_deliveries SET privacy_mode=?,topic_id=?,status='reserved',reserved_at=? WHERE broadcast_id=? AND channel_id=? AND user_id=? AND status='pending'",
                (privacy_mode, topic_id, dt_to_db(utc_now()), broadcast_id, channel_id, user_id),
            )
            if cursor.rowcount != 1:
                await self.conn.rollback()
                return None
            await self.conn.commit()
            return {
                "privacy_mode": privacy_mode,
                "group_id": int(route["group_id"]) if route is not None and route["group_id"] is not None else None,
                "topic_id": topic_id,
                "topic_status": str(route["topic_status"]) if route is not None and route["topic_status"] is not None else None,
            }

    async def complete_broadcast_delivery(self, *, broadcast_id: str, channel_id: int, user_id: int, status: str, error_code: str | None = None) -> bool:
        if status not in {"delivered", "undelivered", "error"}:
            raise ValueError("Invalid broadcast delivery status")
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_broadcast_deliveries SET status=?,error_code=?,completed_at=? WHERE broadcast_id=? AND channel_id=? AND user_id=? AND status='reserved'",
                (status, error_code, dt_to_db(utc_now()), broadcast_id, channel_id, user_id),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def finish_broadcast(self, *, broadcast_id: str, channel_id: int) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_broadcasts SET status='completed',completed_at=?,updated_at=? WHERE broadcast_id=? AND channel_id=? AND status='sending'",
                (dt_to_db(utc_now()), dt_to_db(utc_now()), broadcast_id, channel_id),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def get_broadcast_delivery_summary(self, *, broadcast_id: str, channel_id: int) -> dict[str, int]:
        broadcast = await self.get_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
        if broadcast is None:
            raise ValueError("Unknown broadcast")
        rows = await (await self.conn.execute(
            "SELECT status,COUNT(*) AS count FROM channel_broadcast_deliveries WHERE broadcast_id=? AND channel_id=? GROUP BY status",
            (broadcast_id, channel_id),
        )).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "unique_recipients": int(broadcast["recipient_count"]),
            "delivered": counts.get("delivered", 0),
            "undelivered": counts.get("undelivered", 0),
            "errors": counts.get("error", 0),
            "skipped": counts.get("reserved", 0) + counts.get("pending", 0),
        }

    # ------------------------------------------------------------------
    # Administrator reaction routing. Source mappings are created only for
    # subscriber messages copied into user topics, never for cards/admin text.
    # ------------------------------------------------------------------
    async def get_channel_reaction_settings(self, channel_id: int) -> dict[str, object]:
        row = await (await self.conn.execute(
            "SELECT * FROM channel_reaction_settings WHERE channel_id=?", (channel_id,)
        )).fetchone()
        if row is None:
            return {
                "channel_id": channel_id, "mode": "subscriber", "service_topic_id": None,
                "service_topic_name": None, "requires_repair": False,
            }
        return {
            "channel_id": int(row["channel_id"]),
            "mode": str(row["mode"]),
            "service_topic_id": int(row["service_topic_id"]) if row["service_topic_id"] is not None else None,
            "service_topic_name": str(row["service_topic_name"]) if row["service_topic_name"] is not None else None,
            "requires_repair": bool(row["requires_repair"]),
        }

    async def set_channel_reaction_mode(self, *, channel_id: int, mode: str, updated_by: int) -> None:
        if mode not in {"subscriber", "service"}:
            raise ValueError("Invalid reaction mode")
        current = await self.get_channel_reaction_settings(channel_id)
        if mode == "service" and (current["service_topic_id"] is None or bool(current["requires_repair"])):
            raise ValueError("Service reaction topic is not ready")
        now = dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO channel_reaction_settings(channel_id,mode,updated_at,updated_by) VALUES(?,?,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (channel_id, mode, now, updated_by),
            )
            await self.conn.commit()

    async def set_reaction_service_topic(self, *, channel_id: int, topic_id: int, topic_name: str, updated_by: int, activate: bool = True) -> None:
        name = " ".join(str(topic_name).strip().split())
        if not 1 <= len(name) <= 128 or topic_id <= 0:
            raise ValueError("Invalid reaction service topic")
        now = dt_to_db(utc_now())
        mode = "service" if activate else "subscriber"
        async with self._write_lock:
            await self.conn.execute(
                """INSERT INTO channel_reaction_settings(channel_id,mode,service_topic_id,service_topic_name,requires_repair,updated_at,updated_by)
                   VALUES(?,?,?,?,0,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET mode=excluded.mode,service_topic_id=excluded.service_topic_id,
                     service_topic_name=excluded.service_topic_name,requires_repair=0,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (channel_id, mode, topic_id, name, now, updated_by),
            )
            await self.conn.commit()

    async def rename_reaction_service_topic(self, *, channel_id: int, topic_name: str, updated_by: int) -> bool:
        name = " ".join(str(topic_name).strip().split())
        if not 1 <= len(name) <= 128:
            raise ValueError("Invalid reaction service topic name")
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_reaction_settings SET service_topic_name=?,updated_at=?,updated_by=? WHERE channel_id=? AND service_topic_id IS NOT NULL",
                (name, dt_to_db(utc_now()), updated_by, channel_id),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def mark_reaction_service_topic_unavailable(self, *, channel_id: int) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE channel_reaction_settings SET requires_repair=1,updated_at=? WHERE channel_id=?",
                (dt_to_db(utc_now()), channel_id),
            )
            await self.conn.commit()

    async def record_reaction_source(self, *, channel_id: int, group_id: int, forum_message_id: int, user_id: int, privacy_mode: str, private_chat_id: int, private_message_id: int, topic_id: int) -> None:
        if privacy_mode not in {"identified", "anonymous"}:
            raise ValueError("Invalid privacy mode")
        async with self._write_lock:
            await self.conn.execute(
                """INSERT OR REPLACE INTO channel_reaction_sources(
                       channel_id,group_id,forum_message_id,user_id,privacy_mode,private_chat_id,private_message_id,topic_id,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (channel_id, group_id, forum_message_id, user_id, privacy_mode, private_chat_id, private_message_id, topic_id, dt_to_db(utc_now())),
            )
            await self.conn.commit()

    async def get_reaction_source(self, *, group_id: int, forum_message_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM channel_reaction_sources WHERE group_id=? AND forum_message_id=?",
            (group_id, forum_message_id),
        )).fetchone()

    async def record_reaction_event(self, *, channel_id: int, group_id: int, source_message_id: int, actor_id: int, reaction_key: str, event_at: datetime, mode: str) -> bool:
        if mode not in {"subscriber", "service"}:
            raise ValueError("Invalid reaction mode")
        async with self._write_lock:
            cursor = await self.conn.execute(
                """INSERT OR IGNORE INTO channel_reaction_events(
                       channel_id,group_id,source_message_id,actor_id,reaction_key,event_at,mode,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (channel_id, group_id, source_message_id, actor_id, reaction_key, dt_to_db(event_at), mode, dt_to_db(utc_now())),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def claim_reaction_dispatch(self, *, channel_id: int, group_id: int, source_message_id: int, service_topic_id: int, triggered_by: int, reaction_key: str) -> bool:
        now = dt_to_db(utc_now())
        async with self._write_lock:
            cursor = await self.conn.execute(
                """INSERT OR IGNORE INTO channel_reaction_dispatches(
                       channel_id,group_id,source_message_id,service_topic_id,status,triggered_by,reaction_key,created_at,updated_at
                   ) VALUES(?,?,?,?,'sending',?,?,?,?)""",
                (channel_id, group_id, source_message_id, service_topic_id, triggered_by, reaction_key, now, now),
            )
            if cursor.rowcount == 0:
                cursor = await self.conn.execute(
                    """UPDATE channel_reaction_dispatches SET status='sending',service_topic_id=?,triggered_by=?,reaction_key=?,
                           error_code=NULL,updated_at=?
                       WHERE channel_id=? AND group_id=? AND source_message_id=? AND status='error'""",
                    (service_topic_id, triggered_by, reaction_key, now, channel_id, group_id, source_message_id),
                )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def complete_reaction_dispatch(self, *, channel_id: int, group_id: int, source_message_id: int, destination_message_id: int) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute(
                """UPDATE channel_reaction_dispatches SET status='sent',destination_message_id=?,error_code=NULL,updated_at=?
                   WHERE channel_id=? AND group_id=? AND source_message_id=? AND status='sending'""",
                (destination_message_id, dt_to_db(utc_now()), channel_id, group_id, source_message_id),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def fail_reaction_dispatch(self, *, channel_id: int, group_id: int, source_message_id: int, error_code: str) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute(
                """UPDATE channel_reaction_dispatches SET status='error',error_code=?,updated_at=?
                   WHERE channel_id=? AND group_id=? AND source_message_id=? AND status='sending'""",
                (error_code[:120], dt_to_db(utc_now()), channel_id, group_id, source_message_id),
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Channels. owner_id identifies a human owner; channel_id identifies a
    # concrete configured submission channel.
    # ------------------------------------------------------------------
    async def _custom_pack_foundation_is_active_locked(self) -> bool:
        """Return whether schema v23 is active on this database.

        Migration tests deliberately open older schema versions with the current
        Database class.  In those fixtures register_channel must retain the old
        behaviour.  Once v23 is recorded, however, missing customization tables
        are treated as corruption and setup fails closed rather than creating a
        channel without its required immutable customization snapshot.
        """
        row = await (await self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=23 AND name='custom_pack_foundation'"
        )).fetchone()
        return row is not None

    async def _snapshot_active_standard_for_channel_locked(
        self,
        *,
        channel_id: int,
        owner_id: int,
        created_at: datetime,
    ) -> tuple[int, int]:
        """Copy the active Standard Custom Pack into a new channel.

        The caller must hold ``_write_lock`` and an open write transaction.
        No commit is performed here: channel creation, anonymous counter, custom
        snapshot, state and audit are one atomic unit.
        """
        standard_state = await (await self.conn.execute(
            "SELECT active_revision_id FROM bot_standard_custom_state WHERE singleton_id=1"
        )).fetchone()
        if standard_state is None:
            raise DatabaseMigrationError(
                "Standard Custom Pack state is missing; refusing partial channel setup"
            )
        standard_revision_id = int(standard_state["active_revision_id"])
        standard_revision = await (await self.conn.execute(
            "SELECT 1 FROM bot_standard_custom_revisions WHERE revision_id=?",
            (standard_revision_id,),
        )).fetchone()
        if standard_revision is None:
            raise DatabaseMigrationError(
                "Active Standard Custom Pack revision is missing"
            )
        item_count = int((await (await self.conn.execute(
            "SELECT COUNT(*) AS c FROM bot_standard_custom_items WHERE revision_id=?",
            (standard_revision_id,),
        )).fetchone())["c"])
        if item_count < 1:
            raise DatabaseMigrationError(
                "Active Standard Custom Pack is empty; refusing partial channel setup"
            )

        now_value = dt_to_db(created_at)
        revision_cursor = await self.conn.execute(
            """INSERT INTO channel_custom_revisions(
                   channel_id,source,source_standard_revision_id,created_at,created_by,summary
               ) VALUES(?,?,?,?,?,?)""",
            (
                channel_id,
                "setup_snapshot",
                standard_revision_id,
                now_value,
                owner_id,
                "Initial channel customization snapshot from active Standard Custom Pack",
            ),
        )
        revision_id = int(revision_cursor.lastrowid)
        await self.conn.execute(
            """INSERT INTO channel_custom_items(revision_id,item_key,item_type,payload_json)
               SELECT ?,item_key,item_type,payload_json
               FROM bot_standard_custom_items
               WHERE revision_id=?
               ORDER BY item_key""",
            (revision_id, standard_revision_id),
        )
        await self.conn.execute(
            """INSERT INTO channel_custom_state(
                   channel_id,active_revision_id,initial_revision_id,source_standard_revision_id,updated_at,updated_by
               ) VALUES(?,?,?,?,?,?)""",
            (
                channel_id,
                revision_id,
                revision_id,
                standard_revision_id,
                now_value,
                owner_id,
            ),
        )
        await self.conn.execute(
            """INSERT INTO customization_audit_log(
                   actor_user_id,scope_type,scope_id,channel_id,action,target_key,metadata_json,created_at
               ) VALUES(?,'channel_custom',?,?, 'setup_snapshot',NULL,?,?)""",
            (
                owner_id,
                channel_id,
                channel_id,
                json.dumps(
                    {
                        "revision_id": revision_id,
                        "source_standard_revision_id": standard_revision_id,
                        "items": item_count,
                    },
                    sort_keys=True,
                ),
                now_value,
            ),
        )
        return revision_id, standard_revision_id

    async def register_channel(self, *, owner_id: int, group_id: int, group_title: str, default_reset_days: int, default_notice_text: str, default_timezone: str, anonymous_prefix: str = "Анон") -> tuple[str, sqlite3.Row | None]:
        now = utc_now()
        normalized_prefix = self.normalize_anonymous_prefix(anonymous_prefix)
        async with self._write_lock:
            transaction_started = False
            try:
                # Serialise the ownership/limit check with creation and the
                # Standard Pack snapshot.  This also guarantees that a failed
                # snapshot cannot leave a half-configured channel behind.
                await self.conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                row = await (await self.conn.execute(
                    "SELECT * FROM channels WHERE group_id=?", (group_id,)
                )).fetchone()
                if row is not None:
                    if int(row["owner_id"]) != owner_id:
                        await self.conn.rollback()
                        return "group_has_other_owner", row
                    await self.conn.execute(
                        "UPDATE channels SET group_title=?, updated_at=?, enabled=1 WHERE channel_id=?",
                        (group_title, dt_to_db(now), row["channel_id"]),
                    )
                    await self.conn.commit()
                    return "existing", await (await self.conn.execute(
                        "SELECT * FROM channels WHERE channel_id=?", (row["channel_id"],)
                    )).fetchone()

                count = (await (await self.conn.execute(
                    "SELECT COUNT(*) AS c FROM channels WHERE owner_id=?", (owner_id,)
                )).fetchone())["c"]
                if int(count) >= 5:
                    await self.conn.rollback()
                    return "owner_channel_limit", None

                next_reset = now + timedelta(days=default_reset_days)
                cursor = await self.conn.execute(
                    """INSERT INTO channels(
                           owner_id,group_id,group_title,created_at,updated_at,reset_interval_days,
                           notice_text,timezone_name,next_reset_at,enabled,auto_cleanup_enabled,anonymous_prefix
                       ) VALUES(?,?,?,?,?,?,?,?,?,1,1,?)""",
                    (
                        owner_id, group_id, group_title, dt_to_db(now), dt_to_db(now),
                        default_reset_days, default_notice_text, default_timezone,
                        dt_to_db(next_reset), normalized_prefix,
                    ),
                )
                channel_id = int(cursor.lastrowid)
                await self.conn.execute(
                    "INSERT INTO channel_anonymous_counters(channel_id,next_number,cycle_key) VALUES(?,1,?)",
                    (channel_id, dt_to_db(next_reset)),
                )

                if await self._custom_pack_foundation_is_active_locked():
                    await self._snapshot_active_standard_for_channel_locked(
                        channel_id=channel_id,
                        owner_id=owner_id,
                        created_at=now,
                    )

                await self.conn.commit()
                return "created", await (await self.conn.execute(
                    "SELECT * FROM channels WHERE channel_id=?", (channel_id,)
                )).fetchone()
            except Exception:
                if transaction_started:
                    await self.conn.rollback()
                raise

    async def get_channel_by_id(self, channel_id:int) -> sqlite3.Row|None:
        return await (await self.conn.execute("SELECT * FROM channels WHERE channel_id=? AND enabled=1",(channel_id,))).fetchone()
    async def get_channel_by_group(self, group_id:int) -> sqlite3.Row|None:
        return await (await self.conn.execute("SELECT * FROM channels WHERE group_id=? AND enabled=1",(group_id,))).fetchone()
    async def get_legacy_channel_for_owner(self, owner_id:int) -> sqlite3.Row|None:
        return await (await self.conn.execute("SELECT c.* FROM legacy_owner_channels l JOIN channels c ON c.channel_id=l.channel_id WHERE l.owner_id=? AND c.enabled=1",(owner_id,))).fetchone()
    async def list_enabled_channels(self)->list[sqlite3.Row]:
        return await (await self.conn.execute("SELECT * FROM channels WHERE enabled=1 ORDER BY channel_id")).fetchall()
    async def list_enabled_channels_for_owner(self, owner_id: int) -> list[sqlite3.Row]:
        return await (await self.conn.execute("SELECT * FROM channels WHERE owner_id=? AND enabled=1 ORDER BY channel_id", (owner_id,))).fetchall()
    async def set_active_admin_channel(self, *, owner_id: int, channel_id: int) -> bool:
        channel = await (await self.conn.execute("SELECT 1 FROM channels WHERE channel_id=? AND owner_id=? AND enabled=1", (channel_id, owner_id))).fetchone()
        if channel is None:
            return False
        async with self._write_lock:
            await self.conn.execute("INSERT INTO active_admin_channel(owner_id,channel_id,selected_at) VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET channel_id=excluded.channel_id,selected_at=excluded.selected_at", (owner_id, channel_id, dt_to_db(utc_now())))
            await self.conn.commit()
        return True
    async def get_active_admin_channel(self, owner_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute("SELECT c.* FROM active_admin_channel a JOIN channels c ON c.channel_id=a.channel_id WHERE a.owner_id=? AND c.owner_id=? AND c.enabled=1", (owner_id, owner_id))).fetchone()
    async def set_channel_period(self, channel_id:int, days:int)->datetime:
        if days<2: raise ValueError("Период очистки должен быть не меньше 2 дней")
        next_reset=utc_now()+timedelta(days=days)
        async with self._write_lock:
            await self.conn.execute("UPDATE channels SET reset_interval_days=?,next_reset_at=?,updated_at=? WHERE channel_id=?",(days,dt_to_db(next_reset),dt_to_db(utc_now()),channel_id)); await self.conn.commit()
        return next_reset
    async def set_channel_notice(self, channel_id:int,text:str)->None: await self._update_channel(channel_id,"notice_text",text)
    async def set_channel_topic_template(self, *, channel_id: int, privacy_mode: str, template: str) -> None:
        if privacy_mode not in {"identified", "anonymous"}:
            raise ValueError("Invalid privacy mode")
        column = "identified_topic_template" if privacy_mode == "identified" else "anonymous_topic_template"
        async with self._write_lock:
            await self.conn.execute(f"UPDATE channels SET {column}=?, updated_at=? WHERE channel_id=?", (template, dt_to_db(utc_now()), channel_id))
            await self.conn.commit()
    async def set_channel_timezone(self, channel_id:int,timezone_name:str)->None: await self._update_channel(channel_id,"timezone_name",timezone_name)
    async def _update_channel(self,channel_id:int,column:str,value:str)->None:
        async with self._write_lock:
            await self.conn.execute(f"UPDATE channels SET {column}=?,updated_at=? WHERE channel_id=?",(value,dt_to_db(utc_now()),channel_id)); await self.conn.commit()
    async def set_auto_cleanup_enabled(self,channel_id:int,enabled:bool)->None:
        async with self._write_lock:
            await self.conn.execute("UPDATE channels SET auto_cleanup_enabled=?,updated_at=? WHERE channel_id=?",(int(enabled),dt_to_db(utc_now()),channel_id)); await self.conn.commit()

    async def enable_auto_cleanup(self, *, channel_id: int, days: int) -> datetime:
        if days < 2:
            raise ValueError("Cleanup period must be at least 2 days")
        now = utc_now()
        next_reset = now + timedelta(days=days)
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE channels SET auto_cleanup_enabled=1, reset_interval_days=?, next_reset_at=?, updated_at=? WHERE channel_id=?",
                (days, dt_to_db(next_reset), dt_to_db(now), channel_id),
            )
            await self.conn.commit()
        return next_reset

    async def advance_channel_reset(self,*,channel_id:int,next_reset_at:datetime)->None:
        """Advance a completed cleanup cycle and atomically restart anonymous numbering."""
        next_reset_value = dt_to_db(next_reset_at)
        cycle_key = f"auto:{uuid.uuid4().hex}"
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE channels SET next_reset_at=?,updated_at=? WHERE channel_id=?",
                (next_reset_value,dt_to_db(utc_now()),channel_id),
            )
            cursor = await self.conn.execute(
                "UPDATE channel_anonymous_counters SET next_number=1,cycle_key=? WHERE channel_id=?",
                (cycle_key,channel_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown channel_id")
            await self.conn.commit()

    @staticmethod
    def normalize_anonymous_prefix(value: str) -> str:
        prefix = " ".join(str(value).strip().split())
        if not 1 <= len(prefix) <= 32:
            raise ValueError("Anonymous prefix must contain 1 to 32 characters")
        return prefix

    async def set_channel_anonymous_prefix(self, *, channel_id: int, prefix: str) -> str:
        normalized = self.normalize_anonymous_prefix(prefix)
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channels SET anonymous_prefix=?,updated_at=? WHERE channel_id=?",
                (normalized,dt_to_db(utc_now()),channel_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown channel_id")
            await self.conn.commit()
        return normalized

    async def get_anonymous_counter_state(self, channel_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            """SELECT c.anonymous_prefix, x.next_number, x.cycle_key
               FROM channels c JOIN channel_anonymous_counters x ON x.channel_id=c.channel_id
               WHERE c.channel_id=? AND c.enabled=1""",
            (channel_id,),
        )).fetchone()

    async def reset_anonymous_cycle(self, channel_id: int) -> str:
        cycle_key = f"manual:{uuid.uuid4().hex}"
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_anonymous_counters SET next_number=1,cycle_key=? WHERE channel_id=?",
                (cycle_key,channel_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown channel_id")
            await self.conn.commit()
        return cycle_key

    async def get_privacy_mode(self,*,channel_id:int,user_id:int)->str|None:
        row=await (await self.conn.execute("SELECT privacy_mode FROM channel_subscriber_privacy WHERE channel_id=? AND user_id=?",(channel_id,user_id))).fetchone()
        return str(row["privacy_mode"]) if row else None

    async def _ensure_anonymous_tag_locked(self, *, channel_id: int, user_id: int) -> str:
        state = await (await self.conn.execute(
            """SELECT c.anonymous_prefix,x.cycle_key
               FROM channels c JOIN channel_anonymous_counters x ON x.channel_id=c.channel_id
               WHERE c.channel_id=? AND c.enabled=1""",
            (channel_id,),
        )).fetchone()
        if state is None:
            raise ValueError("Unknown channel_id")
        cycle_key = str(state["cycle_key"])
        existing = await (await self.conn.execute(
            "SELECT tag FROM anonymous_tags WHERE channel_id=? AND user_id=? AND cycle_key=?",
            (channel_id,user_id,cycle_key),
        )).fetchone()
        if existing is not None:
            return str(existing["tag"])
        row = await (await self.conn.execute(
            "UPDATE channel_anonymous_counters SET next_number=next_number+1 WHERE channel_id=? RETURNING next_number-1 AS number",
            (channel_id,),
        )).fetchone()
        if row is None:
            raise ValueError("Unknown channel_id")
        number = int(row["number"])
        tag = f"{state['anonymous_prefix']}-{number}"
        await self.conn.execute(
            "INSERT INTO anonymous_tags(channel_id,user_id,cycle_key,number,tag,assigned_at) VALUES(?,?,?,?,?,?)",
            (channel_id,user_id,cycle_key,number,tag,dt_to_db(utc_now())),
        )
        return tag

    async def ensure_anonymous_tag(self, *, channel_id: int, user_id: int) -> str:
        async with self._write_lock:
            tag = await self._ensure_anonymous_tag_locked(channel_id=channel_id,user_id=user_id)
            await self.conn.commit()
        return tag

    async def set_privacy_mode(self,*,channel_id:int,user_id:int,privacy_mode:str)->str|None:
        if privacy_mode not in {"identified","anonymous"}: raise ValueError("Invalid privacy mode")
        async with self._write_lock:
            await self.conn.execute("INSERT INTO channel_subscriber_privacy(channel_id,user_id,privacy_mode,updated_at) VALUES(?,?,?,?) ON CONFLICT(channel_id,user_id) DO UPDATE SET privacy_mode=excluded.privacy_mode,updated_at=excluded.updated_at",(channel_id,user_id,privacy_mode,dt_to_db(utc_now())))
            tag = await self._ensure_anonymous_tag_locked(channel_id=channel_id,user_id=user_id) if privacy_mode == "anonymous" else None
            await self.conn.commit()
            return tag

    async def get_anonymous_tag(self,*,channel_id:int,user_id:int)->str|None:
        row=await (await self.conn.execute(
            """SELECT t.tag FROM anonymous_tags t
               JOIN channel_anonymous_counters x ON x.channel_id=t.channel_id AND x.cycle_key=t.cycle_key
               WHERE t.channel_id=? AND t.user_id=?""",
            (channel_id,user_id),
        )).fetchone()
        return str(row["tag"]) if row else None

    async def upsert_user(self,*,user_id:int,first_name:str,last_name:str|None,username:str|None)->None:
        now=dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute("INSERT INTO users(user_id,first_name,last_name,username,first_seen_at,last_seen_at,blocked) VALUES(?,?,?,?,?,?,0) ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,last_name=excluded.last_name,username=excluded.username,last_seen_at=excluded.last_seen_at",(user_id,first_name,last_name,username,now,now)); await self.conn.commit()
    async def set_user_blocked(self,user_id:int,blocked:bool)->None:
        async with self._write_lock: await self.conn.execute("UPDATE users SET blocked=? WHERE user_id=?",(int(blocked),user_id)); await self.conn.commit()
    async def attach_subscriber(self,*,channel_id:int,user_id:int)->None:
        now=dt_to_db(utc_now())
        async with self._write_lock:
            await self.conn.execute("INSERT INTO channel_subscribers(channel_id,user_id,first_seen_at,last_seen_at) VALUES(?,?,?,?) ON CONFLICT(channel_id,user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",(channel_id,user_id,now,now))
            await self.conn.execute("INSERT INTO active_channel(user_id,channel_id,selected_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET channel_id=excluded.channel_id,selected_at=excluded.selected_at",(user_id,channel_id,now)); await self.conn.commit()
    async def touch_subscriber(self,*,channel_id:int,user_id:int)->None:
        async with self._write_lock: await self.conn.execute("UPDATE channel_subscribers SET last_seen_at=? WHERE channel_id=? AND user_id=?",(dt_to_db(utc_now()),channel_id,user_id)); await self.conn.commit()
    async def get_active_channel_for_user(self,user_id:int)->sqlite3.Row|None:
        return await (await self.conn.execute("SELECT c.* FROM active_channel a JOIN channels c ON c.channel_id=a.channel_id WHERE a.user_id=? AND c.enabled=1",(user_id,))).fetchone()
    async def list_enabled_channels_for_user(self,user_id:int)->list[sqlite3.Row]:
        return await (await self.conn.execute("SELECT c.* FROM channel_subscribers s JOIN channels c ON c.channel_id=s.channel_id WHERE s.user_id=? AND c.enabled=1 ORDER BY c.group_title COLLATE NOCASE, c.channel_id",(user_id,))).fetchall()
    async def set_active_channel(self,*,user_id:int,channel_id:int)->bool:
        async with self._write_lock:
            row=await (await self.conn.execute("SELECT 1 FROM channel_subscribers s JOIN channels c ON c.channel_id=s.channel_id WHERE s.user_id=? AND s.channel_id=? AND c.enabled=1",(user_id,channel_id))).fetchone()
            if row is None: return False
            await self.conn.execute("INSERT INTO active_channel(user_id,channel_id,selected_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET channel_id=excluded.channel_id,selected_at=excluded.selected_at",(user_id,channel_id,dt_to_db(utc_now())))
            await self.conn.commit(); return True
    async def count_channel_subscribers(self,channel_id:int)->int:
        return int((await (await self.conn.execute("SELECT COUNT(*) AS c FROM channel_subscribers WHERE channel_id=?",(channel_id,))).fetchone())["c"])
    async def get_unnotified_subscribers(self,*,channel_id:int,cycle_at:str)->list[int]:
        rows=await (await self.conn.execute("SELECT s.user_id FROM channel_subscribers s LEFT JOIN channel_notification_log n ON n.channel_id=s.channel_id AND n.user_id=s.user_id AND n.cycle_at=? WHERE s.channel_id=? AND n.user_id IS NULL ORDER BY s.user_id",(cycle_at,channel_id))).fetchall(); return [int(r["user_id"]) for r in rows]
    async def mark_notification_sent(self,*,channel_id:int,cycle_at:str,user_id:int)->None:
        async with self._write_lock: await self.conn.execute("INSERT OR IGNORE INTO channel_notification_log(channel_id,cycle_at,user_id,sent_at) VALUES(?,?,?,?)",(channel_id,cycle_at,user_id,dt_to_db(utc_now()))); await self.conn.commit()
    async def get_topic_for_user(self,*,channel_id:int,user_id:int,privacy_mode:str="identified")->sqlite3.Row|None: return await (await self.conn.execute("SELECT * FROM channel_topics WHERE channel_id=? AND user_id=? AND privacy_mode=?",(channel_id,user_id,privacy_mode))).fetchone()
    async def get_subscriber_card_data(self, *, channel_id: int, user_id: int, privacy_mode: str) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            """SELECT s.first_seen_at, s.last_seen_at,
                      (SELECT COUNT(*) FROM message_events e WHERE e.channel_id=s.channel_id AND e.user_id=s.user_id AND e.privacy_mode=? AND e.direction='subscriber_to_admin') AS message_count,
                      t.tag AS anonymous_tag
               FROM channel_subscribers s
               LEFT JOIN anonymous_tags t ON t.channel_id=s.channel_id AND t.user_id=s.user_id
                    AND t.cycle_key=(SELECT cycle_key FROM channel_anonymous_counters WHERE channel_id=s.channel_id)
               WHERE s.channel_id=? AND s.user_id=?""",
            (privacy_mode, channel_id, user_id),
        )).fetchone()
    async def get_topic_by_group_thread(self,*,group_id:int,topic_id:int)->sqlite3.Row|None: return await (await self.conn.execute("SELECT * FROM channel_topics WHERE group_id=? AND topic_id=?",(group_id,topic_id))).fetchone()
    @staticmethod
    def _like_literal(query: str) -> str:
        return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def search_subscribers(self, *, channel_id: int, query: str, privacy_filter: str = "all", offset: int = 0, limit: int = 8, conversations_only: bool = False) -> tuple[list[dict[str, object]], int]:
        normalized = " ".join(query.strip().split())
        if not normalized or len(normalized) > 96 or privacy_filter not in {"all", "identified", "anonymous"}:
            raise ValueError("Invalid search query")
        pattern = f"%{self._like_literal(normalized.lstrip('@'))}%"
        mode_clause = "" if privacy_filter == "all" else " AND COALESCE(p.privacy_mode,'identified')=?"
        mode_params: list[object] = [] if privacy_filter == "all" else [privacy_filter]
        base = f"""SELECT s.user_id,p.privacy_mode,u.first_name,u.last_name,u.username,t.tag,ct.status,ct.topic_id,ct.group_id
FROM channel_subscribers s JOIN users u ON u.user_id=s.user_id
LEFT JOIN channel_subscriber_privacy p ON p.channel_id=s.channel_id AND p.user_id=s.user_id
LEFT JOIN anonymous_tags t ON t.channel_id=s.channel_id AND t.user_id=s.user_id AND t.cycle_key=(SELECT cycle_key FROM channel_anonymous_counters WHERE channel_id=s.channel_id)
LEFT JOIN channel_topics ct ON ct.channel_id=s.channel_id AND ct.user_id=s.user_id AND ct.privacy_mode=COALESCE(p.privacy_mode,'identified')
WHERE s.channel_id=? {mode_clause} {' AND ct.topic_id IS NOT NULL' if conversations_only else ''} AND ((COALESCE(p.privacy_mode,'identified')='anonymous' AND EXISTS (SELECT 1 FROM anonymous_tags previous_tag WHERE previous_tag.channel_id=s.channel_id AND previous_tag.user_id=s.user_id AND previous_tag.tag LIKE ? ESCAPE '\\')) OR (COALESCE(p.privacy_mode,'identified')='identified' AND (u.first_name LIKE ? ESCAPE '\\' OR u.last_name LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\' OR CAST(u.user_id AS TEXT)=?)))"""
        args=[channel_id,*mode_params,pattern,pattern,pattern,pattern,normalized]
        rows=await (await self.conn.execute(base+" ORDER BY CASE WHEN COALESCE(p.privacy_mode,'identified')='anonymous' THEN t.tag ELSE u.first_name END COLLATE NOCASE, s.user_id LIMIT ? OFFSET ?",[*args,limit,offset])).fetchall()
        total=int((await (await self.conn.execute("SELECT COUNT(*) c FROM ("+base+")",args)).fetchone())['c'])
        out=[]
        for r in rows:
            anonymous=str(r['privacy_mode'] or 'identified')=='anonymous'
            out.append({'user_id':int(r['user_id']),'privacy_mode':'anonymous' if anonymous else 'identified','display_name':str(r['tag'] or 'Анонимная подписчица') if anonymous else ' '.join(x for x in (str(r['first_name'] or ''),str(r['last_name'] or '')) if x).strip() or 'Подписчица','status':str(r['status']) if r['status'] else None,'topic_id':int(r['topic_id']) if r['topic_id'] else None,'group_id':int(r['group_id']) if r['group_id'] else None})
        return out,total

    async def get_subscriber_moderation(self, *, channel_id: int, user_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute("SELECT * FROM channel_subscriber_moderation WHERE channel_id=? AND user_id=?", (channel_id, user_id))).fetchone()
    @staticmethod
    def resolve_sanction_reason(choice: str | None, custom_reason: str | None = None) -> str:
        if choice not in SANCTION_REASON_CHOICES:
            raise ValueError("A sanction reason is required")
        if choice == "other":
            text = (custom_reason or "").strip()
            if not text:
                raise ValueError("A custom sanction reason is required")
            return text[:1000]
        return SANCTION_REASON_LABELS[str(choice)]

    async def apply_subscriber_sanction(self, *, channel_id: int, user_id: int, admin_id: int, action: str, reason_choice: str | None, custom_reason: str | None = None, show_reason_to_subscriber: bool = False, rate_limit_seconds: int | None = None, muted_until: datetime | None = None, blocked_until: datetime | None = None, permanently_blocked: bool | None = None, duration_seconds: int | None = None) -> str:
        if action not in SANCTION_ACTIONS:
            raise ValueError("Unknown sanction action")
        if type(show_reason_to_subscriber) is not bool:
            raise ValueError("Invalid reason visibility")
        reason = self.resolve_sanction_reason(reason_choice, custom_reason)
        now = utc_now()
        expires_at: datetime | None = None
        rate_seconds: int | None = None
        if action == "rate_limit":
            rate_seconds = rate_limit_seconds if rate_limit_seconds is not None else duration_seconds
            if not isinstance(rate_seconds, int) or rate_seconds < 1:
                raise ValueError("A positive rate limit is required")
            # A rate limit is an ongoing per-channel policy, not a one-shot
            # temporary block.  It stays active until an administrator revokes
            # it; each accepted subscriber publication starts the next interval.
            expires_at = None
            await self.update_subscriber_moderation(channel_id=channel_id, user_id=user_id, rate_limit_seconds=rate_seconds, sanction_reason=reason, show_reason_to_subscriber=show_reason_to_subscriber)
        elif action == "mute":
            expires_at = muted_until or (now + timedelta(seconds=duration_seconds or 60))
            if expires_at <= now:
                raise ValueError("A future mute expiry is required")
            await self.update_subscriber_moderation(channel_id=channel_id, user_id=user_id, muted_until=expires_at, sanction_reason=reason, show_reason_to_subscriber=show_reason_to_subscriber)
        elif action == "temporary_block":
            expires_at = blocked_until or (now + timedelta(seconds=duration_seconds or 60))
            if expires_at <= now:
                raise ValueError("A future block expiry is required")
            await self.update_subscriber_moderation(channel_id=channel_id, user_id=user_id, blocked_until=expires_at, sanction_reason=reason, show_reason_to_subscriber=show_reason_to_subscriber)
        elif action == "permanent_block":
            await self.update_subscriber_moderation(channel_id=channel_id, user_id=user_id, permanently_blocked=True, sanction_reason=reason, show_reason_to_subscriber=show_reason_to_subscriber)
        # A warning intentionally has no active restriction state. Reapplying
        # the same restriction replaces only the currently effective instance,
        # so an older sanction cannot unexpectedly reappear after a newer one.
        async with self._write_lock:
            if action != "warning":
                await self.conn.execute(
                    "UPDATE subscriber_sanctions SET active=0,revoked_at=?,revoked_by=? "
                    "WHERE channel_id=? AND user_id=? AND action=? AND active=1 "
                    "AND (expires_at IS NULL OR expires_at>?)",
                    (dt_to_db(now), admin_id, channel_id, user_id, action, dt_to_db(now)),
                )
            await self.conn.execute("INSERT INTO subscriber_sanctions(channel_id,user_id,action,rate_limit_seconds,expires_at,reason,show_reason_to_subscriber,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (channel_id,user_id,action,rate_seconds,dt_to_db(expires_at) if expires_at else None,reason,int(show_reason_to_subscriber),0 if action == "warning" else 1,dt_to_db(now)))
            await self.conn.commit()
        await self.record_moderation_action(channel_id=channel_id, user_id=user_id, admin_id=admin_id, action=action, reason=reason, expires_at=expires_at, details=None, show_reason_to_subscriber=show_reason_to_subscriber, created_at=now)
        return reason

    async def list_active_sanctions(self, *, channel_id: int, user_id: int, now: datetime | None = None) -> list[sqlite3.Row]:
        moment = dt_to_db(now or utc_now())
        return await (await self.conn.execute("SELECT * FROM subscriber_sanctions WHERE channel_id=? AND user_id=? AND active=1 AND action != 'warning' AND (expires_at IS NULL OR expires_at>?) ORDER BY CASE action WHEN 'permanent_block' THEN 1 WHEN 'temporary_block' THEN 2 WHEN 'mute' THEN 3 WHEN 'rate_limit' THEN 4 ELSE 9 END, sanction_id DESC", (channel_id,user_id,moment))).fetchall()

    async def get_effective_subscriber_sanction(self, *, channel_id: int, user_id: int, now: datetime | None = None) -> sqlite3.Row | None:
        rows = await self.list_active_sanctions(channel_id=channel_id, user_id=user_id, now=now)
        return rows[0] if rows else None

    async def revoke_active_sanctions(self, *, channel_id: int, user_id: int, admin_id: int) -> int:
        now = utc_now()
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE subscriber_sanctions SET active=0,revoked_at=?,revoked_by=? "
                "WHERE channel_id=? AND user_id=? AND active=1 AND action != 'warning' "
                "AND (expires_at IS NULL OR expires_at>?)",
                (dt_to_db(now),admin_id,channel_id,user_id,dt_to_db(now)),
            )
            count = cursor.rowcount
            await self.conn.commit()
        if count:
            await self.update_subscriber_moderation(channel_id=channel_id,user_id=user_id,rate_limit_seconds=0,muted_until=now,blocked_until=now,permanently_blocked=False)
            await self.record_moderation_action(channel_id=channel_id,user_id=user_id,admin_id=admin_id,action="clear_restrictions",details=str(count),created_at=now)
        return count

    async def record_moderation_action(self, *, channel_id: int, user_id: int, admin_id: int, action: str, reason: str | None = None, expires_at: datetime | None = None, details: str | None = None, show_reason_to_subscriber: bool = False, created_at: datetime | None = None) -> None:
        async with self._write_lock:
            await self.conn.execute("INSERT INTO moderation_log(channel_id,user_id,admin_id,action,reason,expires_at,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (channel_id,user_id,admin_id,action,reason,dt_to_db(expires_at) if expires_at else None,details,int(show_reason_to_subscriber),dt_to_db(created_at or utc_now())))
            await self.conn.commit()
    async def list_moderation_actions(self, *, channel_id: int, user_id: int) -> list[sqlite3.Row]:
        return await (await self.conn.execute("SELECT * FROM moderation_log WHERE channel_id=? AND user_id=? ORDER BY log_id DESC", (channel_id,user_id))).fetchall()
    async def get_subscriber_moderation_history(self, *, channel_id: int, user_id: int, offset: int = 0, limit: int = 10, now: datetime | None = None) -> list[dict[str, object]]:
        if offset < 0 or not 1 <= limit <= 50: raise ValueError("Invalid history page")
        moment=now or utc_now(); rows=await (await self.conn.execute("SELECT sanction_id AS item_id,action,reason,show_reason_to_subscriber,expires_at,active,created_at,revoked_at,revoked_by,rate_limit_seconds,(SELECT admin_id FROM moderation_log m WHERE m.channel_id=subscriber_sanctions.channel_id AND m.user_id=subscriber_sanctions.user_id AND m.action=subscriber_sanctions.action AND m.created_at=subscriber_sanctions.created_at ORDER BY log_id DESC LIMIT 1) AS admin_id FROM subscriber_sanctions WHERE channel_id=? AND user_id=? UNION ALL SELECT -log_id AS item_id,action,reason,show_reason_to_subscriber,expires_at,0 AS active,created_at,NULL,NULL,NULL,admin_id FROM moderation_log WHERE channel_id=? AND user_id=? AND action NOT IN ('rate_limit','mute','temporary_block','permanent_block','warning') ORDER BY created_at DESC,item_id DESC LIMIT ? OFFSET ?",(channel_id,user_id,channel_id,user_id,limit,offset))).fetchall()
        result=[]
        for row in rows:
            expires=dt_from_db(str(row['expires_at'])) if row['expires_at'] else None
            status='warning' if row['action']=='warning' else ('removed' if row['revoked_at'] else ('expired' if expires and expires<=moment else ('active' if row['active'] else 'historical')))
            result.append({**dict(row),'status':status})
        return result

    async def count_subscriber_moderation_history(self, *, channel_id:int, user_id:int) -> int:
        row=await (await self.conn.execute("SELECT (SELECT COUNT(*) FROM subscriber_sanctions WHERE channel_id=? AND user_id=?) + (SELECT COUNT(*) FROM moderation_log WHERE channel_id=? AND user_id=? AND action NOT IN ('rate_limit','mute','temporary_block','permanent_block','warning')) AS count",(channel_id,user_id,channel_id,user_id))).fetchone(); return int(row['count'])

    async def _require_channel_subscriber(self, *, channel_id: int, user_id: int) -> None:
        row = await (await self.conn.execute(
            "SELECT 1 FROM channel_subscribers WHERE channel_id=? AND user_id=?",
            (channel_id, user_id),
        )).fetchone()
        if row is None:
            raise ValueError("Subscriber is not attached to this channel")

    @staticmethod
    def _normalize_subscriber_metadata_text(value: str, *, limit: int) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Metadata text must not be blank")
        if len(normalized) > limit:
            raise ValueError("Metadata text is too long")
        return normalized

    async def add_subscriber_note(self, *, channel_id: int, user_id: int, admin_id: int, note_text: str) -> int:
        note = self._normalize_subscriber_metadata_text(note_text, limit=1000)
        await self._require_channel_subscriber(channel_id=channel_id, user_id=user_id)
        async with self._write_lock:
            cursor = await self.conn.execute(
                "INSERT INTO subscriber_notes(channel_id,user_id,admin_id,note_text,created_at) VALUES(?,?,?,?,?)",
                (channel_id, user_id, admin_id, note, dt_to_db(utc_now())),
            )
            note_id = int(cursor.lastrowid)
            await self.conn.execute(
                "INSERT INTO moderation_log(channel_id,user_id,admin_id,action,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)",
                (channel_id, user_id, admin_id, "note_added", f"note_id={note_id}", 0, dt_to_db(utc_now())),
            )
            await self.conn.commit()
        return note_id

    async def list_subscriber_notes(self, *, channel_id: int, user_id: int, offset: int = 0, limit: int = 10) -> list[sqlite3.Row]:
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Invalid notes page")
        return await (await self.conn.execute(
            "SELECT * FROM subscriber_notes WHERE channel_id=? AND user_id=? AND deleted_at IS NULL ORDER BY note_id DESC LIMIT ? OFFSET ?",
            (channel_id, user_id, limit, offset),
        )).fetchall()

    async def count_subscriber_notes(self, *, channel_id: int, user_id: int) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) AS count FROM subscriber_notes WHERE channel_id=? AND user_id=? AND deleted_at IS NULL",
            (channel_id, user_id),
        )).fetchone()
        return int(row["count"])

    async def get_subscriber_note(self, *, channel_id: int, user_id: int, note_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM subscriber_notes WHERE channel_id=? AND user_id=? AND note_id=? AND deleted_at IS NULL",
            (channel_id, user_id, note_id),
        )).fetchone()

    async def update_subscriber_note(self, *, channel_id: int, user_id: int, note_id: int, admin_id: int, note_text: str) -> bool:
        note = self._normalize_subscriber_metadata_text(note_text, limit=1000)
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE subscriber_notes SET note_text=?,updated_at=?,updated_by=? WHERE channel_id=? AND user_id=? AND note_id=? AND deleted_at IS NULL",
                (note, dt_to_db(utc_now()), admin_id, channel_id, user_id, note_id),
            )
            if cursor.rowcount != 1:
                await self.conn.rollback()
                return False
            await self.conn.execute(
                "INSERT INTO moderation_log(channel_id,user_id,admin_id,action,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)",
                (channel_id, user_id, admin_id, "note_updated", f"note_id={note_id}", 0, dt_to_db(utc_now())),
            )
            await self.conn.commit()
        return True

    async def soft_delete_subscriber_note(self, *, channel_id: int, user_id: int, note_id: int, admin_id: int) -> bool:
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE subscriber_notes SET deleted_at=?,deleted_by=? WHERE channel_id=? AND user_id=? AND note_id=? AND deleted_at IS NULL",
                (dt_to_db(utc_now()), admin_id, channel_id, user_id, note_id),
            )
            if cursor.rowcount != 1:
                await self.conn.rollback()
                return False
            await self.conn.execute(
                "INSERT INTO moderation_log(channel_id,user_id,admin_id,action,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)",
                (channel_id, user_id, admin_id, "note_deleted", f"note_id={note_id}", 0, dt_to_db(utc_now())),
            )
            await self.conn.commit()
        return True

    async def add_subscriber_tag(self, *, channel_id: int, user_id: int, admin_id: int, tag: str) -> bool:
        normalized = self._normalize_subscriber_metadata_text(tag, limit=64)
        tag_key = normalized.casefold()
        await self._require_channel_subscriber(channel_id=channel_id, user_id=user_id)
        async with self._write_lock:
            cursor = await self.conn.execute(
                "INSERT OR IGNORE INTO subscriber_tags(channel_id,user_id,tag,tag_key,added_by,created_at) VALUES(?,?,?,?,?,?)",
                (channel_id, user_id, normalized, tag_key, admin_id, dt_to_db(utc_now())),
            )
            created = cursor.rowcount == 1
            if created:
                await self.conn.execute(
                    "INSERT INTO moderation_log(channel_id,user_id,admin_id,action,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)",
                    (channel_id, user_id, admin_id, "tag_added", normalized, 0, dt_to_db(utc_now())),
                )
            await self.conn.commit()
        return created

    async def list_subscriber_tags(self, *, channel_id: int, user_id: int, offset: int = 0, limit: int = 20) -> list[sqlite3.Row]:
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Invalid tags page")
        return await (await self.conn.execute(
            "SELECT * FROM subscriber_tags WHERE channel_id=? AND user_id=? ORDER BY tag COLLATE NOCASE,tag_id LIMIT ? OFFSET ?",
            (channel_id, user_id, limit, offset),
        )).fetchall()

    async def count_subscriber_tags(self, *, channel_id: int, user_id: int) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) AS count FROM subscriber_tags WHERE channel_id=? AND user_id=?",
            (channel_id, user_id),
        )).fetchone()
        return int(row["count"])

    async def get_subscriber_tag(self, *, channel_id: int, user_id: int, tag_id: int) -> sqlite3.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM subscriber_tags WHERE channel_id=? AND user_id=? AND tag_id=?",
            (channel_id, user_id, tag_id),
        )).fetchone()

    async def delete_subscriber_tag(self, *, channel_id: int, user_id: int, tag_id: int, admin_id: int) -> bool:
        async with self._write_lock:
            row = await (await self.conn.execute(
                "SELECT tag FROM subscriber_tags WHERE channel_id=? AND user_id=? AND tag_id=?",
                (channel_id, user_id, tag_id),
            )).fetchone()
            if row is None:
                return False
            cursor = await self.conn.execute(
                "DELETE FROM subscriber_tags WHERE channel_id=? AND user_id=? AND tag_id=?",
                (channel_id, user_id, tag_id),
            )
            if cursor.rowcount != 1:
                await self.conn.rollback()
                return False
            await self.conn.execute(
                "INSERT INTO moderation_log(channel_id,user_id,admin_id,action,details,show_reason_to_subscriber,created_at) VALUES(?,?,?,?,?,?,?)",
                (channel_id, user_id, admin_id, "tag_deleted", str(row["tag"]), 0, dt_to_db(utc_now())),
            )
            await self.conn.commit()
        return True

    async def get_last_subscriber_message_at(self, *, channel_id: int, user_id: int) -> datetime | None:
        row = await (await self.conn.execute(
            "SELECT occurred_at FROM message_events WHERE channel_id=? AND user_id=? "
            "AND direction='subscriber_to_admin' ORDER BY occurred_at DESC,event_id DESC LIMIT 1",
            (channel_id, user_id),
        )).fetchone()
        return dt_from_db(str(row["occurred_at"])) if row is not None else None

    async def _rate_limit_next_allowed_at(self, *, sanction: sqlite3.Row, channel_id: int, user_id: int) -> datetime | None:
        seconds = sanction["rate_limit_seconds"]
        if not isinstance(seconds, int) or seconds < 1:
            return None
        last_message = await self.get_last_subscriber_message_at(channel_id=channel_id, user_id=user_id)
        return last_message + timedelta(seconds=seconds) if last_message is not None else None

    async def active_subscriber_restriction(self, *, channel_id: int, user_id: int, now: datetime | None = None) -> tuple[str, datetime | None] | None:
        moment = now or utc_now()
        sanction = await self.get_effective_subscriber_sanction(channel_id=channel_id,user_id=user_id,now=moment)
        if sanction is not None:
            action = str(sanction["action"])
            if action == "rate_limit":
                next_allowed = await self._rate_limit_next_allowed_at(
                    sanction=sanction, channel_id=channel_id, user_id=user_id
                )
                return ("rate_limited", next_allowed) if next_allowed is not None and next_allowed > moment else None
            mapping={"permanent_block":"permanently_blocked","temporary_block":"blocked","mute":"muted"}
            raw=sanction["expires_at"]
            return mapping[action], dt_from_db(str(raw)) if raw else None
        # Compatibility fallback for callers that still set the pre-v13 state directly.
        state = await self.get_subscriber_moderation(channel_id=channel_id, user_id=user_id)
        if state is None:
            return None
        if bool(state["permanently_blocked"]): return "permanently_blocked", None
        for kind, field in (("blocked", "blocked_until"), ("muted", "muted_until")):
            if state[field] and dt_from_db(str(state[field])) > moment: return kind, dt_from_db(str(state[field]))
        if state["rate_limit_seconds"]:
            until=dt_from_db(str(state["updated_at"]))+timedelta(seconds=int(state["rate_limit_seconds"]))
            if until>moment: return "rate_limited",until
        return None

    async def active_subscriber_restriction_details(self, *, channel_id: int, user_id: int, now: datetime | None = None) -> tuple[str, datetime | None, str | None, bool] | None:
        moment=now or utc_now()
        sanction=await self.get_effective_subscriber_sanction(channel_id=channel_id,user_id=user_id,now=moment)
        if sanction is not None:
            action = str(sanction["action"])
            if action == "rate_limit":
                next_allowed = await self._rate_limit_next_allowed_at(
                    sanction=sanction, channel_id=channel_id, user_id=user_id
                )
                if next_allowed is None or next_allowed <= moment:
                    return None
                return "rate_limited", next_allowed, str(sanction["reason"]), bool(sanction["show_reason_to_subscriber"])
            mapping={"permanent_block":"permanently_blocked","temporary_block":"blocked","mute":"muted"}
            return mapping[action],dt_from_db(str(sanction["expires_at"])) if sanction["expires_at"] else None,str(sanction["reason"]),bool(sanction["show_reason_to_subscriber"])
        restriction=await self.active_subscriber_restriction(channel_id=channel_id,user_id=user_id,now=moment)
        if restriction is None:return None
        state=await self.get_subscriber_moderation(channel_id=channel_id,user_id=user_id)
        return restriction[0],restriction[1],state["sanction_reason"] if state else None,bool(state and state["show_reason_to_subscriber"])

    async def update_subscriber_moderation(self, *, channel_id: int, user_id: int, rate_limit_seconds: int | None = None, muted_until: datetime | None = None, blocked_until: datetime | None = None, permanently_blocked: bool | None = None, marked_spam: bool | None = None, internal_note: str | None = None, sanction_reason: str | None = None, show_reason_to_subscriber: bool | None = None) -> None:
        existing = await self.get_subscriber_moderation(channel_id=channel_id, user_id=user_id)
        values = {
            "rate_limit_seconds": rate_limit_seconds if rate_limit_seconds is not None else (existing["rate_limit_seconds"] if existing else None),
            "muted_until": dt_to_db(muted_until) if muted_until else (existing["muted_until"] if existing else None),
            "blocked_until": dt_to_db(blocked_until) if blocked_until else (existing["blocked_until"] if existing else None),
            "permanently_blocked": int(permanently_blocked) if permanently_blocked is not None else (existing["permanently_blocked"] if existing else 0),
            "marked_spam": int(marked_spam) if marked_spam is not None else (existing["marked_spam"] if existing else 0),
            "internal_note": internal_note if internal_note is not None else (existing["internal_note"] if existing else None),
            "sanction_reason": sanction_reason if sanction_reason is not None else (existing["sanction_reason"] if existing else None),
            "show_reason_to_subscriber": int(show_reason_to_subscriber) if show_reason_to_subscriber is not None else (existing["show_reason_to_subscriber"] if existing else 0),
        }
        async with self._write_lock:
            await self.conn.execute("""INSERT INTO channel_subscriber_moderation(channel_id,user_id,rate_limit_seconds,muted_until,blocked_until,permanently_blocked,marked_spam,internal_note,sanction_reason,show_reason_to_subscriber,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,user_id) DO UPDATE SET rate_limit_seconds=excluded.rate_limit_seconds,muted_until=excluded.muted_until,blocked_until=excluded.blocked_until,permanently_blocked=excluded.permanently_blocked,marked_spam=excluded.marked_spam,internal_note=excluded.internal_note,sanction_reason=excluded.sanction_reason,show_reason_to_subscriber=excluded.show_reason_to_subscriber,updated_at=excluded.updated_at""", (channel_id, user_id, values["rate_limit_seconds"], values["muted_until"], values["blocked_until"], values["permanently_blocked"], values["marked_spam"], values["internal_note"], values["sanction_reason"], values["show_reason_to_subscriber"], dt_to_db(utc_now())))
            await self.conn.commit()
    async def create_topic_mapping(self,*,channel_id:int,user_id:int,group_id:int,topic_id:int,privacy_mode:str="identified")->None:
        now=dt_to_db(utc_now())
        async with self._write_lock: await self.conn.execute("INSERT INTO channel_topics(channel_id,user_id,privacy_mode,group_id,topic_id,created_at,last_activity_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(channel_id,user_id,privacy_mode) DO UPDATE SET group_id=excluded.group_id,topic_id=excluded.topic_id,created_at=excluded.created_at,last_activity_at=excluded.last_activity_at",(channel_id,user_id,privacy_mode,group_id,topic_id,now,now)); await self.conn.commit()
    async def touch_topic(self,*,channel_id:int,user_id:int,privacy_mode:str="identified")->None:
        async with self._write_lock: await self.conn.execute("UPDATE channel_topics SET last_activity_at=? WHERE channel_id=? AND user_id=? AND privacy_mode=?",(dt_to_db(utc_now()),channel_id,user_id,privacy_mode)); await self.conn.commit()
    async def delete_topic_mapping(self,*,channel_id:int,user_id:int,privacy_mode:str="identified")->None:
        async with self._write_lock: await self.conn.execute("DELETE FROM channel_topics WHERE channel_id=? AND user_id=? AND privacy_mode=?",(channel_id,user_id,privacy_mode)); await self.conn.commit()
    async def set_channel_cleanup_policy(self, *, channel_id: int, basis: str, status_scope: str, action: str, final_delete_days: int = 7) -> None:
        if basis not in {"created_at", "last_activity_at"}:
            raise ValueError("Invalid cleanup basis")
        if status_scope not in {"all", "answered_closed"}:
            raise ValueError("Invalid cleanup status scope")
        if action not in {"delete", "close", "close_then_delete"}:
            raise ValueError("Invalid cleanup action")
        if final_delete_days < 1:
            raise ValueError("Final deletion delay must be positive")
        await self._update_channel_cleanup_policy(channel_id, basis, status_scope, action, final_delete_days)

    async def _update_channel_cleanup_policy(self, channel_id: int, basis: str, status_scope: str, action: str, final_delete_days: int) -> None:
        async with self._write_lock:
            await self.conn.execute("UPDATE channels SET cleanup_basis=?, cleanup_status_scope=?, cleanup_action=?, cleanup_final_delete_days=?, updated_at=? WHERE channel_id=?", (basis, status_scope, action, final_delete_days, dt_to_db(utc_now()), channel_id))
            await self.conn.commit()

    async def topics_due_for_auto_cleanup(self, *, channel, cutoff: datetime, now: datetime) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        channel_id = int(channel["channel_id"])
        basis = str(channel["cleanup_basis"])
        scope = str(channel["cleanup_status_scope"])
        action = str(channel["cleanup_action"])
        basis_column = "created_at" if basis == "created_at" else "last_activity_at"
        status_filter = "" if scope == "all" else " AND status IN ('answered', 'closed')"
        eligible = "status != 'in_progress' AND is_important = 0 AND is_pinned = 0"
        close_rows: list[sqlite3.Row] = []
        delete_rows: list[sqlite3.Row] = []
        if action in {"delete", "close"}:
            cursor = await self.conn.execute(f"SELECT * FROM channel_topics WHERE channel_id=? AND {basis_column}<? AND {eligible}{status_filter} AND auto_closed_at IS NULL ORDER BY {basis_column}", (channel_id, dt_to_db(cutoff)))
            rows = await cursor.fetchall()
            if action == "delete": delete_rows = rows
            else: close_rows = rows
        else:
            cursor = await self.conn.execute(f"SELECT * FROM channel_topics WHERE channel_id=? AND {basis_column}<? AND {eligible}{status_filter} AND auto_closed_at IS NULL ORDER BY {basis_column}", (channel_id, dt_to_db(cutoff)))
            close_rows = await cursor.fetchall()
            final_cutoff = now - timedelta(days=int(channel["cleanup_final_delete_days"]))
            cursor = await self.conn.execute(f"SELECT * FROM channel_topics WHERE channel_id=? AND auto_closed_at<? AND {eligible}{status_filter} ORDER BY auto_closed_at", (channel_id, dt_to_db(final_cutoff)))
            delete_rows = await cursor.fetchall()
        return close_rows, delete_rows

    async def mark_topic_auto_closed(self, *, channel_id: int, user_id: int, privacy_mode: str, closed_at: datetime | None = None) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE channel_topics SET auto_closed_at=?, status='closed' WHERE channel_id=? AND user_id=? AND privacy_mode=?",
                (dt_to_db(closed_at or utc_now()), channel_id, user_id, privacy_mode),
            )
            await self.conn.commit()

    async def topics_created_before(self, *, channel_id: int, cutoff: datetime) -> list[sqlite3.Row]:
        cursor = await self.conn.execute("SELECT * FROM channel_topics WHERE channel_id=? AND created_at<? AND status != 'in_progress' AND is_important=0 AND is_pinned=0 ORDER BY created_at", (channel_id, dt_to_db(cutoff)))
        return await cursor.fetchall()
    async def set_topic_status(self, *, channel_id:int, user_id:int, privacy_mode:str, status:str) -> bool:
        if status not in {"new","in_progress","answered","closed"}: raise ValueError("Invalid topic status")
        async with self._write_lock:
            cursor=await self.conn.execute("UPDATE channel_topics SET status=? WHERE channel_id=? AND user_id=? AND privacy_mode=?",(status,channel_id,user_id,privacy_mode))
            await self.conn.commit(); return cursor.rowcount == 1

    async def mark_topic_answered(self, *, channel_id: int, user_id: int, privacy_mode: str) -> bool:
        """Auto-mark a conversation answered without reopening a manually closed case."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE channel_topics SET status='answered' WHERE channel_id=? AND user_id=? AND privacy_mode=? AND status!='closed'",
                (channel_id, user_id, privacy_mode),
            )
            await self.conn.commit()
            return cursor.rowcount == 1
    async def set_topic_cleanup_protection(self, *, channel_id: int, user_id: int, privacy_mode: str, important: bool | None = None, pinned: bool | None = None) -> bool:
        if important is None and pinned is None:
            raise ValueError("At least one protection flag is required")
        fields: list[str] = []
        values: list[int] = []
        if important is not None:
            fields.append("is_important = ?")
            values.append(int(important))
        if pinned is not None:
            fields.append("is_pinned = ?")
            values.append(int(pinned))
        values.extend((channel_id, user_id, privacy_mode))
        async with self._write_lock:
            cursor = await self.conn.execute(
                f"UPDATE channel_topics SET {', '.join(fields)} WHERE channel_id = ? AND user_id = ? AND privacy_mode = ?",
                values,
            )
            await self.conn.commit()
            return cursor.rowcount == 1

    async def record_message_event(self, *, channel_id:int, user_id:int, privacy_mode:str, direction:str, message_type:str, occurred_at:datetime, source_chat_id:int, source_message_id:int, admin_id:int|None=None, media_group_id:str|None=None, conversation_id:int|None=None) -> None:
        if privacy_mode not in {"identified", "anonymous"}: raise ValueError("Invalid privacy mode")
        if direction not in {"subscriber_to_admin", "admin_to_subscriber"}: raise ValueError("Invalid event direction")
        async with self._write_lock:
            await self.conn.execute("INSERT OR IGNORE INTO message_events(channel_id,user_id,privacy_mode,direction,message_type,occurred_at,source_chat_id,source_message_id,admin_id,media_group_id,conversation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(channel_id,user_id,privacy_mode,direction,message_type,dt_to_db(occurred_at),source_chat_id,source_message_id,admin_id,media_group_id,conversation_id))
            await self.conn.commit()

    async def get_subscriber_statistics(self, *, channel_id: int, user_id: int, timezone_name: str, now: datetime | None = None) -> dict[str, object]:
        moment=now or utc_now(); channel=await self.get_channel_by_id(channel_id)
        if channel is None: raise ValueError("Unknown channel")
        subscriber=await (await self.conn.execute("SELECT * FROM channel_subscribers WHERE channel_id=? AND user_id=?",(channel_id,user_id))).fetchone()
        if subscriber is None: raise ValueError("Unknown channel subscriber")
        events=await (await self.conn.execute("SELECT * FROM message_events WHERE channel_id=? AND user_id=? ORDER BY occurred_at,event_id",(channel_id,user_id))).fetchall()
        incoming=[e for e in events if e['direction']=='subscriber_to_admin']; outgoing=[e for e in events if e['direction']=='admin_to_subscriber']
        local_zone=ZoneInfo(timezone_name)
        media={key:0 for key in ('text','photo','video','document','voice','audio','sticker','other')}
        for e in incoming: media[e['message_type'] if e['message_type'] in media else 'other']+=1
        conversation_ids={int(e['conversation_id']) for e in events if e['conversation_id'] is not None}
        topics=await (await self.conn.execute("SELECT status,topic_id FROM channel_topics WHERE channel_id=? AND user_id=?",(channel_id,user_id))).fetchall()
        conversation_count=max(len(conversation_ids),len(topics))
        answered_ids={int(e['conversation_id']) for e in outgoing if e['conversation_id'] is not None}
        if not conversation_ids: answered_count=sum(1 for row in topics if row['status'] in ('answered','closed'))
        else: answered_count=len(answered_ids)
        closed_count=sum(1 for row in topics if row['status']=='closed')
        first_responses=[]
        for cid in conversation_ids:
            received=[dt_from_db(e['occurred_at']) for e in incoming if e['conversation_id']==cid]
            replies=[dt_from_db(e['occurred_at']) for e in outgoing if e['conversation_id']==cid]
            if received and replies:
                after=[r for r in replies if r>=min(received)]
                if after: first_responses.append((min(after)-min(received)).total_seconds())
        active_days={dt_from_db(e['occurred_at']).astimezone(local_zone).date() for e in incoming}
        weekdays=[dt_from_db(e['occurred_at']).astimezone(local_zone).weekday() for e in incoming]
        hours=[dt_from_db(e['occurred_at']).astimezone(local_zone).hour for e in incoming]
        moderation=await self.list_moderation_actions(channel_id=channel_id,user_id=user_id)
        active=await self.list_active_sanctions(channel_id=channel_id,user_id=user_id,now=moment)
        return {'first_activity':subscriber['first_seen_at'],'last_activity':subscriber['last_seen_at'],'active_days':len(active_days),'subscriber_messages':len(incoming),'admin_replies':len(outgoing),'conversations':conversation_count,'answered_conversations':answered_count,'closed_conversations':closed_count,'average_messages_per_conversation':round(len(incoming)/conversation_count,2) if conversation_count else 0.0,'media':media,'average_first_response_seconds':round(sum(first_responses)/len(first_responses),2) if first_responses else None,'median_first_response_seconds':median(first_responses) if first_responses else None,'answered_percentage':round(answered_count*100/conversation_count,1) if conversation_count else 0.0,'last_7_days':sum(dt_from_db(e['occurred_at'])>=moment-timedelta(days=7) for e in incoming),'last_30_days':sum(dt_from_db(e['occurred_at'])>=moment-timedelta(days=30) for e in incoming),'active_weekday':max(set(weekdays),key=weekdays.count) if weekdays else None,'active_hour':max(set(hours),key=hours.count) if hours else None,'moderation':{'warnings':sum(r['action']=='warning' for r in moderation),'restrictions':sum(r['action'] in SANCTION_ACTIONS and r['action']!='warning' for r in moderation),'active_restrictions':len(active),'spam_marks':sum(r['action']=='mark_spam' for r in moderation),'notes':await self.count_subscriber_notes(channel_id=channel_id,user_id=user_id),'tags':await self.count_subscriber_tags(channel_id=channel_id,user_id=user_id)}}

    async def get_channel_statistics(
        self,
        channel_id: int,
        *,
        period: str = "all",
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return one channel-scoped, metadata-only statistics snapshot.

        Conversation metrics intentionally use only events that have a known
        conversation_id.  This keeps post-migration measurements honest when
        older journal rows cannot be linked to a forum topic retrospectively.
        """
        if period not in {"today", "7d", "30d", "all"}:
            raise ValueError("Unsupported statistics period")
        channel = await self.get_channel_by_id(channel_id)
        if channel is None:
            raise ValueError("Unknown channel")

        moment = (now or utc_now()).astimezone(timezone.utc)
        local_zone = ZoneInfo(str(channel["timezone_name"]))
        if period == "today":
            local_now = moment.astimezone(local_zone)
            start_utc = datetime.combine(
                local_now.date(), datetime.min.time(), tzinfo=local_zone
            ).astimezone(timezone.utc)
        elif period == "7d":
            start_utc = moment - timedelta(days=7)
        elif period == "30d":
            start_utc = moment - timedelta(days=30)
        else:
            start_utc = None

        event_sql = "SELECT * FROM message_events WHERE channel_id = ? AND occurred_at <= ?"
        parameters: list[object] = [channel_id, dt_to_db(moment)]
        if start_utc is not None:
            event_sql += " AND occurred_at >= ?"
            parameters.append(dt_to_db(start_utc))
        event_sql += " ORDER BY occurred_at ASC, event_id ASC"
        events = await (await self.conn.execute(event_sql, parameters)).fetchall()
        incoming = [row for row in events if row["direction"] == "subscriber_to_admin"]
        outgoing = [row for row in events if row["direction"] == "admin_to_subscriber"]

        def activity_cutoff(days: int) -> str:
            return dt_to_db(moment - timedelta(days=days))

        active_row = await (await self.conn.execute(
            """SELECT
                COUNT(DISTINCT CASE WHEN occurred_at >= ? THEN user_id END) AS active_1d,
                COUNT(DISTINCT CASE WHEN occurred_at >= ? THEN user_id END) AS active_7d,
                COUNT(DISTINCT CASE WHEN occurred_at >= ? THEN user_id END) AS active_30d
               FROM message_events
               WHERE channel_id = ? AND direction = 'subscriber_to_admin' AND occurred_at <= ?""",
            (activity_cutoff(1), activity_cutoff(7), activity_cutoff(30), channel_id, dt_to_db(moment)),
        )).fetchone()
        unique_subscribers = await self.count_channel_subscribers(channel_id)

        new_sql = "SELECT COUNT(*) AS count FROM channel_subscribers WHERE channel_id = ? AND first_seen_at <= ?"
        new_parameters: list[object] = [channel_id, dt_to_db(moment)]
        if start_utc is not None:
            new_sql += " AND first_seen_at >= ?"
            new_parameters.append(dt_to_db(start_utc))
        new_subscribers = int((await (await self.conn.execute(new_sql, new_parameters)).fetchone())["count"])

        incoming_by_conversation: dict[int, list[datetime]] = {}
        outgoing_by_conversation: dict[int, list[datetime]] = {}
        legacy_conversation_events = False
        for row in events:
            conversation_id = row["conversation_id"]
            if conversation_id is None:
                legacy_conversation_events = True
                continue
            target = (
                incoming_by_conversation
                if row["direction"] == "subscriber_to_admin"
                else outgoing_by_conversation
            )
            target.setdefault(int(conversation_id), []).append(dt_from_db(row["occurred_at"]))

        # A conversation begins with a subscriber message.  Replies that
        # precede it, or belong to another conversation, cannot be responses.
        conversation_ids = set(incoming_by_conversation)
        response_seconds: list[float] = []
        answered_conversations = 0
        for conversation_id in conversation_ids:
            first_subscriber_message = min(incoming_by_conversation[conversation_id])
            replies_after_start = [
                reply for reply in outgoing_by_conversation.get(conversation_id, [])
                if reply >= first_subscriber_message
            ]
            if replies_after_start:
                answered_conversations += 1
                response_seconds.append(
                    (min(replies_after_start) - first_subscriber_message).total_seconds()
                )
        conversation_count = len(conversation_ids)

        media = {key: 0 for key in (
            "text", "photo", "video", "document", "voice", "audio", "sticker", "other",
        )}
        media_item_types = {"photo", "video", "document", "voice", "audio", "sticker", "animation"}
        album_ids: set[str] = set()
        media_items_count = 0
        messages_by_hour = {hour: 0 for hour in range(24)}
        messages_by_weekday = {weekday: 0 for weekday in range(7)}
        for row in incoming:
            message_type = str(row["message_type"])
            media[message_type if message_type in media else "other"] += 1
            if message_type in media_item_types:
                media_items_count += 1
            if row["media_group_id"]:
                album_ids.add(str(row["media_group_id"]))
            local_time = dt_from_db(row["occurred_at"]).astimezone(local_zone)
            messages_by_hour[local_time.hour] += 1
            messages_by_weekday[local_time.weekday()] += 1
        most_active_hour = (
            min(messages_by_hour, key=lambda hour: (-messages_by_hour[hour], hour))
            if incoming else None
        )
        most_active_weekday = (
            min(messages_by_weekday, key=lambda weekday: (-messages_by_weekday[weekday], weekday))
            if incoming else None
        )
        top_subscribers = await self._build_channel_top_subscribers(
            channel_id=channel_id,
            incoming_events=incoming,
        )

        return {
            "channel_id": channel_id,
            "period": period,
            "period_start_utc": dt_to_db(start_utc) if start_utc else None,
            "timezone": str(channel["timezone_name"]),
            "unique_subscribers": unique_subscribers,
            "active_subscribers_1d": int(active_row["active_1d"] or 0),
            "active_subscribers_7d": int(active_row["active_7d"] or 0),
            "active_subscribers_30d": int(active_row["active_30d"] or 0),
            "new_subscribers": new_subscribers,
            "subscriber_messages": len(incoming),
            "admin_replies": len(outgoing),
            "average_messages_per_subscriber": (
                round(len(incoming) / unique_subscribers, 2)
                if unique_subscribers else 0.0
            ),
            "conversation_count": conversation_count,
            "answered_conversation_count": answered_conversations,
            "answered_conversation_share": (
                round(answered_conversations * 100 / conversation_count, 1)
                if conversation_count else 0.0
            ),
            "average_first_response_seconds": (
                round(sum(response_seconds) / len(response_seconds), 2)
                if response_seconds else None
            ),
            "median_first_response_seconds": (
                median(response_seconds) if response_seconds else None
            ),
            "conversation_metrics_complete": not legacy_conversation_events,
            "media": media,
            "album_count": len(album_ids),
            "media_items_count": media_items_count,
            "messages_by_hour": messages_by_hour,
            "messages_by_weekday": messages_by_weekday,
            "most_active_hour": most_active_hour,
            "most_active_weekday": most_active_weekday,
            "top_subscribers": top_subscribers,
        }

    async def _build_channel_top_subscribers(
        self,
        *,
        channel_id: int,
        incoming_events: Sequence[sqlite3.Row],
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Build presentation-safe Top-N data from already period-filtered events."""
        counts: dict[tuple[int, str], int] = {}
        first_event: dict[tuple[int, str], tuple[datetime, int]] = {}
        for event in incoming_events:
            key = (int(event["user_id"]), str(event["privacy_mode"]))
            counts[key] = counts.get(key, 0) + 1
            event_key = (dt_from_db(event["occurred_at"]), int(event["event_id"]))
            if key not in first_event or event_key < first_event[key]:
                first_event[key] = event_key
        ordered = sorted(
            counts,
            key=lambda key: (-counts[key], first_event[key], key[1], key[0]),
        )[:limit]
        result: list[dict[str, object]] = []
        for user_id, privacy_mode in ordered:
            if privacy_mode == "anonymous":
                tag = await self.get_anonymous_tag(channel_id=channel_id, user_id=user_id)
                safe_tag = tag or "Анонимная подписчица"
                result.append({
                    "privacy_mode": "anonymous",
                    "anonymous_tag": safe_tag,
                    "display_name": safe_tag,
                    "message_count": counts[(user_id, privacy_mode)],
                })
                continue
            user = await (await self.conn.execute(
                "SELECT first_name, last_name, username FROM users WHERE user_id=?",
                (user_id,),
            )).fetchone()
            name_parts = [str(user["first_name"])] if user and user["first_name"] else []
            if user and user["last_name"]:
                name_parts.append(str(user["last_name"]))
            display_name = " ".join(name_parts) or "Подписчица"
            result.append({
                "privacy_mode": "identified",
                "display_name": display_name,
                "message_count": counts[(user_id, privacy_mode)],
            })
        return result

    async def get_channel_admin_statistics(self, channel_id: int, *, period: str = "all", now: datetime | None = None) -> dict[str, object]:
        """Channel-scoped admin metrics from message and moderation metadata only."""
        base = await self.get_channel_statistics(channel_id, period=period, now=now)
        moment = (now or utc_now()).astimezone(timezone.utc)
        start = base["period_start_utc"]
        sql = "SELECT * FROM message_events WHERE channel_id=? AND occurred_at<=?"
        params: list[object] = [channel_id, dt_to_db(moment)]
        if start:
            sql += " AND occurred_at>=?"; params.append(start)
        sql += " ORDER BY occurred_at,event_id"
        events = await (await self.conn.execute(sql, params)).fetchall()
        replies = [r for r in events if r["direction"] == "admin_to_subscriber" and r["admin_id"] is not None]
        moderation_sql = "SELECT * FROM moderation_log WHERE channel_id=? AND created_at<=?"
        moderation_params: list[object] = [channel_id, dt_to_db(moment)]
        if start:
            moderation_sql += " AND created_at>=?"; moderation_params.append(start)
        moderation = await (await self.conn.execute(moderation_sql, moderation_params)).fetchall()
        metrics: dict[int, dict[str, object]] = {}
        def row(admin_id: int) -> dict[str, object]:
            return metrics.setdefault(admin_id, {"admin_id": admin_id, "reply_count": 0, "conversations_replied": set(), "first_response_count": 0, "first_response_samples": [], "moderation_actions": 0, "restrictions": 0, "warnings": 0, "spam_marks": 0, "management_actions": 0})
        incoming: dict[int, list[datetime]] = {}
        outgoing: dict[int, list[tuple[datetime, int]]] = {}
        legacy = False
        for event in events:
            cid = event["conversation_id"]
            if cid is None:
                legacy = True; continue
            if event["direction"] == "subscriber_to_admin":
                incoming.setdefault(int(cid), []).append(dt_from_db(event["occurred_at"]))
            elif event["admin_id"] is not None:
                outgoing.setdefault(int(cid), []).append((dt_from_db(event["occurred_at"]), int(event["admin_id"])))
        for reply in replies:
            admin = row(int(reply["admin_id"])); admin["reply_count"] = int(admin["reply_count"]) + 1
            if reply["conversation_id"] is not None: admin["conversations_replied"].add(int(reply["conversation_id"]))
        for cid, received in incoming.items():
            after = [(at, admin) for at, admin in outgoing.get(cid, []) if at >= min(received)]
            if after:
                at, admin_id = min(after, key=lambda item: (item[0], item[1]))
                admin = row(admin_id); admin["first_response_count"] = int(admin["first_response_count"]) + 1
                admin["first_response_samples"].append((at - min(received)).total_seconds())
        restriction_actions = {"rate_limit", "mute", "temporary_block", "permanent_block"}
        for action in moderation:
            admin = row(int(action["admin_id"])); name = str(action["action"])
            admin["moderation_actions"] = int(admin["moderation_actions"]) + 1
            if name in restriction_actions: admin["restrictions"] = int(admin["restrictions"]) + 1
            elif name == "warning": admin["warnings"] = int(admin["warnings"]) + 1
            elif name == "mark_spam": admin["spam_marks"] = int(admin["spam_marks"]) + 1
            else: admin["management_actions"] = int(admin["management_actions"]) + 1
        result = []
        for admin_id, item in metrics.items():
            user = await (await self.conn.execute("SELECT first_name,last_name FROM users WHERE user_id=?", (admin_id,))).fetchone()
            display = " ".join(str(user[key]) for key in ("first_name", "last_name") if user and user[key]) or "Бывший администратор"
            samples = item.pop("first_response_samples")
            conversations = item.pop("conversations_replied")
            # A conversation is considered handled by the administrator who
            # sent its first valid reply.  This gives one deterministic owner
            # to every answered conversation without inventing responsibility
            # for conversations that nobody answered.
            item.update({
                "display_name": display,
                "unique_conversations_replied": len(conversations),
                "handled_conversations": int(item["first_response_count"]),
                "average_first_response_seconds": round(sum(samples)/len(samples),2) if samples else None,
                "median_first_response_seconds": median(samples) if samples else None,
            })
            result.append(item)
        result.sort(key=lambda item: (-int(item["reply_count"]), -int(item["first_response_count"]), int(item["admin_id"])))
        team_samples = [sample for item in result for sample in ([] if item["average_first_response_seconds"] is None else [])]
        # Rebuild exact team samples from conversations; per-admin rounded averages are not suitable.
        exact_samples=[]
        answered_conversation_count = 0
        for cid, received in incoming.items():
            after=[at for at, _ in outgoing.get(cid, []) if at >= min(received)]
            if after:
                answered_conversation_count += 1
                exact_samples.append((min(after)-min(received)).total_seconds())
        tracked_conversation_count = len(incoming)
        unanswered_conversation_count = tracked_conversation_count - answered_conversation_count
        return {
            "channel_id": channel_id,
            "period": period,
            "admins": result,
            "active_admin_count": len(result),
            "admin_replies": len(replies),
            "tracked_conversation_count": tracked_conversation_count,
            "handled_conversation_count": answered_conversation_count,
            "unanswered_conversation_count": unanswered_conversation_count,
            "team_average_first_response_seconds": round(sum(exact_samples)/len(exact_samples),2) if exact_samples else None,
            "team_median_first_response_seconds": median(exact_samples) if exact_samples else None,
            "top_reply_admin": result[0]["display_name"] if result else None,
            "top_first_response_admin": (sorted(result, key=lambda item: (-int(item["first_response_count"]), -int(item["reply_count"]), int(item["admin_id"])))[0]["display_name"] if result else None),
            "conversation_metrics_complete": not legacy,
        }

    async def get_channel_export_snapshot(self, channel_id: int, *, period: str = "all", now: datetime | None = None) -> dict[str, object]:
        """Return a single, channel-scoped metadata snapshot for future CSV/XLSX export.

        This deliberately composes the established statistics APIs instead of
        introducing independent export formulas or storing message content.
        """
        channel = await self.get_channel_by_id(channel_id)
        if channel is None:
            raise ValueError("Unknown channel")
        generated_at = (now or utc_now()).astimezone(timezone.utc)
        statistics = await self.get_channel_statistics(channel_id, period=period, now=generated_at)
        administrators = await self.get_channel_admin_statistics(channel_id, period=period, now=generated_at)
        return {
            "channel_id": channel_id,
            "period": period,
            "metadata": {"channel_title": str(channel["group_title"]), "timezone": str(channel["timezone_name"]), "generated_at": dt_to_db(generated_at)},
            "statistics": statistics,
            "administrators": administrators,
            "conversation_metrics_complete": bool(statistics["conversation_metrics_complete"]),
        }

    async def count_channel_topics(self,channel_id:int)->int: return int((await (await self.conn.execute("SELECT COUNT(*) AS c FROM channel_topics WHERE channel_id=?",(channel_id,))).fetchone())["c"])
