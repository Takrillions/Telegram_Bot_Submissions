from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class BackupError(RuntimeError):
    pass


class RemoteBackupStore(Protocol):
    def upload(self, local_path: Path) -> str: ...
    def retain(self, keep: int) -> None: ...


def _load_env_file(path: str | Path | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class BackupSettings:
    database_path: Path
    backup_dir: Path
    local_keep: int
    remote_bucket: str
    remote_prefix: str
    remote_keep: int

    @classmethod
    def from_env(cls) -> "BackupSettings":
        _load_env_file(os.getenv("ENV_FILE", "").strip() or None)
        database_path = Path(os.getenv("DATABASE_PATH", "feedback_bot.sqlite3").strip())
        backup_dir = Path(os.getenv("DATABASE_BACKUP_DIR", "backups").strip())
        local_keep = int(os.getenv("DATABASE_BACKUP_KEEP", "7"))
        remote_keep = int(os.getenv("DATABASE_REMOTE_BACKUP_KEEP", "14"))
        if local_keep < 1:
            raise BackupError("DATABASE_BACKUP_KEEP must be at least 1")
        if remote_keep < 1:
            raise BackupError("DATABASE_REMOTE_BACKUP_KEEP must be at least 1")
        return cls(
            database_path=database_path,
            backup_dir=backup_dir,
            local_keep=local_keep,
            remote_bucket=os.getenv("DATABASE_REMOTE_BACKUP_BUCKET", "").strip(),
            remote_prefix=os.getenv("DATABASE_REMOTE_BACKUP_PREFIX", "telegram-bot/sqlite").strip().strip("/"),
            remote_keep=remote_keep,
        )


def verify_sqlite_database(path: str | Path) -> None:
    source = Path(path)
    if not source.is_file():
        raise BackupError(f"SQLite file does not exist: {source}")
    try:
        connection = sqlite3.connect(f"{source.absolute().as_uri()}?mode=ro", uri=True)
        try:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise BackupError("SQLite integrity_check failed: " + "; ".join(integrity))
            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            if foreign_keys:
                raise BackupError(f"SQLite foreign_key_check found {len(foreign_keys)} broken references")
        finally:
            connection.close()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to verify SQLite database: {exc}") from exc


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip())
    return cleaned[:64] or "snapshot"


def _backup_prefix(database_path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", database_path.stem) or "database"
    digest = hashlib.sha256(str(database_path.absolute()).encode("utf-8")).hexdigest()[:12]
    return f"{stem}_{digest}"


def create_verified_snapshot(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    label: str,
) -> Path:
    source_path = Path(database_path)
    if not source_path.is_file():
        raise BackupError(f"Database file does not exist: {source_path}")
    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{_backup_prefix(source_path)}_{_safe_label(label)}_{timestamp}_{uuid.uuid4().hex[:12]}.sqlite3"
    destination = destination_dir / filename
    temporary = destination.with_suffix(".sqlite3.tmp")
    try:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.replace(temporary, destination)
        verify_sqlite_database(destination)
        return destination
    except BackupError:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as exc:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise BackupError(f"Unable to create SQLite snapshot: {exc}") from exc


def retain_local_snapshots(backup_dir: str | Path, *, database_path: str | Path, keep: int) -> list[Path]:
    if keep < 1:
        raise BackupError("Local backup retention must be at least 1")
    directory = Path(backup_dir)
    if not directory.exists():
        return []
    prefix = _backup_prefix(Path(database_path)) + "_"
    snapshots = sorted(
        (item for item in directory.glob(f"{prefix}*.sqlite3") if item.is_file()),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )
    removed: list[Path] = []
    for stale in snapshots[keep:]:
        stale.unlink()
        removed.append(stale)
    return removed


def restore_verified_snapshot(snapshot_path: str | Path, database_path: str | Path) -> None:
    snapshot = Path(snapshot_path)
    destination = Path(database_path)
    verify_sqlite_database(snapshot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".restore.tmp")
    temporary.unlink(missing_ok=True)
    try:
        source = sqlite3.connect(f"{snapshot.absolute().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        verify_sqlite_database(temporary)
        if destination.exists():
            try:
                os.chmod(temporary, destination.stat().st_mode & 0o777)
            except OSError:
                pass
        os.replace(temporary, destination)
        Path(str(destination) + "-wal").unlink(missing_ok=True)
        Path(str(destination) + "-shm").unlink(missing_ok=True)
        verify_sqlite_database(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class GoogleCloudStorageBackupStore:
    def __init__(self, *, bucket_name: str, prefix: str) -> None:
        if not bucket_name:
            raise BackupError("DATABASE_REMOTE_BACKUP_BUCKET is not configured")
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise BackupError("google-cloud-storage is required for remote backups") from exc
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._prefix = prefix.strip("/")

    def _blob_name(self, filename: str) -> str:
        return f"{self._prefix}/{filename}" if self._prefix else filename

    def upload(self, local_path: Path) -> str:
        blob_name = self._blob_name(local_path.name)
        blob = self._bucket.blob(blob_name)
        try:
            blob.upload_from_filename(str(local_path), content_type="application/vnd.sqlite3", timeout=120)
        except Exception as exc:
            raise BackupError(f"Remote backup upload failed: {exc}") from exc
        return f"gs://{self._bucket.name}/{blob_name}"

    def retain(self, keep: int) -> None:
        if keep < 1:
            raise BackupError("Remote backup retention must be at least 1")
        prefix = f"{self._prefix}/" if self._prefix else None
        try:
            blobs = [blob for blob in self._client.list_blobs(self._bucket, prefix=prefix) if blob.name.endswith(".sqlite3")]
            blobs.sort(key=lambda blob: (blob.time_created or datetime.min.replace(tzinfo=timezone.utc), blob.name), reverse=True)
            for stale in blobs[keep:]:
                stale.delete(timeout=60)
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError(f"Remote backup retention failed: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BackupResult:
    local_path: Path
    remote_uri: str | None


def perform_backup(
    settings: BackupSettings,
    *,
    label: str,
    require_remote: bool,
    remote_store: RemoteBackupStore | None = None,
) -> BackupResult:
    local_path = create_verified_snapshot(settings.database_path, settings.backup_dir, label=label)
    remote_uri: str | None = None
    try:
        store = remote_store
        if store is None and settings.remote_bucket:
            store = GoogleCloudStorageBackupStore(bucket_name=settings.remote_bucket, prefix=settings.remote_prefix)
        if require_remote and store is None:
            raise BackupError("Remote backup is required but DATABASE_REMOTE_BACKUP_BUCKET is not configured")
        if store is not None:
            remote_uri = store.upload(local_path)
            store.retain(settings.remote_keep)
        retain_local_snapshots(settings.backup_dir, database_path=settings.database_path, keep=settings.local_keep)
        return BackupResult(local_path=local_path, remote_uri=remote_uri)
    except Exception:
        # Keep the verified local snapshot even when remote storage is unavailable.
        raise


def _main() -> int:
    parser = argparse.ArgumentParser(description="Verified SQLite backup/restore helper")
    parser.add_argument("--label", default="daily")
    parser.add_argument("--remote-required", action="store_true")
    parser.add_argument("--restore-backup", default="")
    args = parser.parse_args()
    settings = BackupSettings.from_env()
    if args.restore_backup:
        restore_verified_snapshot(args.restore_backup, settings.database_path)
        print(f"RESTORED_DATABASE={settings.database_path}")
        return 0
    result = perform_backup(settings, label=args.label, require_remote=args.remote_required)
    print(f"BACKUP_PATH={result.local_path}")
    print(f"REMOTE_OBJECT={result.remote_uri or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
