import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from database import CURRENT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    release_id: str
    commit_sha: str
    created_at: str
    known_schema_version: int
    requirements_hash: str
    deployment_status: str
    migration_applied: bool = False

    @classmethod
    def create(cls, release_id: str, commit_sha: str, requirements_hash: str) -> "ReleaseMetadata":
        return cls(release_id, commit_sha, datetime.now(timezone.utc).isoformat(), CURRENT_SCHEMA_VERSION, requirements_hash, "preparing")


def write_json_atomic(path: str | Path, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def write_release_metadata(release_dir: str | Path, metadata: ReleaseMetadata) -> None:
    write_json_atomic(Path(release_dir) / "release.json", asdict(metadata))


def read_release_metadata(release_dir: str | Path) -> ReleaseMetadata:
    return ReleaseMetadata(**json.loads((Path(release_dir) / "release.json").read_text(encoding="utf-8")))


def update_release_status(release_dir: str | Path, status: str, *, migration_applied: bool | None = None) -> ReleaseMetadata:
    metadata = read_release_metadata(release_dir)
    updated = ReleaseMetadata(metadata.release_id, metadata.commit_sha, metadata.created_at, metadata.known_schema_version, metadata.requirements_hash, status, metadata.migration_applied if migration_applied is None else migration_applied)
    write_release_metadata(release_dir, updated)
    return updated


def clear_readiness(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


def write_readiness(path: str | Path, *, release_id: str, bot_id: int, bot_username: str | None, scheduler_ready: bool, polling_ready: bool) -> None:
    write_json_atomic(path, {"release_id": release_id, "pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat(), "schema_version": CURRENT_SCHEMA_VERSION, "bot_id": bot_id, "bot_username": bot_username, "database_ready": True, "scheduler_ready": scheduler_ready, "polling_ready": polling_ready})


def readiness_is_current(path: str | Path, release_id: str, pid: int | None = None) -> bool:
    try:
        marker = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return marker.get("release_id") == release_id and marker.get("database_ready") is True and marker.get("scheduler_ready") is True and marker.get("polling_ready") is True and (pid is None or marker.get("pid") == pid)


def auto_rollback_allowed(*, migration_applied: bool, previous_known_schema_version: int, database_schema_version: int) -> bool:
    return not migration_applied and previous_known_schema_version >= database_schema_version


def retain_releases(root: str | Path, *, current_release: str, keep: int = 5) -> list[Path]:
    base = Path(root).resolve()
    releases = (base / "releases").resolve()
    if releases.parent != base or keep < 1:
        raise ValueError("Invalid release retention configuration")
    candidates = [item for item in releases.iterdir() if item.is_dir() and item.name != current_release]
    candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    removed=[]
    for item in candidates[keep - 1:]:
        if item.parent.resolve() != releases:
            continue
        shutil.rmtree(item)
        removed.append(item)
    return removed
