#!/usr/bin/env bash
set -euo pipefail
ROOT="${DEPLOY_ROOT:-/home/takrillions/telegram-bot}"
SERVICE="${SERVICE_NAME:-telegram-bot}"
ARCHIVE="${1:?archive path required}"
RELEASE_ID="${2:?release id required}"
COMMIT_SHA="${3:?commit sha required}"
RELEASES="$ROOT/releases"; SHARED="$ROOT/shared"; RELEASE="$RELEASES/$RELEASE_ID"
LOCK="$SHARED/deploy.lock"; READY="$SHARED/runtime/readiness.json"
mkdir -p "$RELEASES" "$SHARED/runtime"
exec 9>"$LOCK"; flock -n 9 || { echo "another deployment is active"; exit 1; }
OLD_CURRENT=""; [ -L "$ROOT/current" ] && OLD_CURRENT="$(readlink -f "$ROOT/current")"
MIGRATED=0; SERVICE_STOPPED=0
fail() {
  local status=$?
  [ -d "$RELEASE" ] && "$RELEASE/.venv/bin/python" -c "from release_runtime import update_release_status; update_release_status('$RELEASE','failed', migration_applied=$MIGRATED)" || true
  if [ "$MIGRATED" = 0 ] && [ "$SERVICE_STOPPED" = 1 ]; then
    if [ -n "$OLD_CURRENT" ] && [ "$(readlink -f "$ROOT/current" 2>/dev/null || true)" = "$RELEASE" ]; then
      ln -s "$OLD_CURRENT" "$ROOT/current.rollback"; mv -Tf "$ROOT/current.rollback" "$ROOT/current"
    fi
    systemctl start "$SERVICE" || true
  fi
  if [ "$MIGRATED" = 1 ]; then echo "migration completed; automatic rollback is forbidden; manual decision required" >&2; fi
  exit "$status"
}
trap fail ERR
[ ! -e "$RELEASE" ]; mkdir -p "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
python3 -m venv "$RELEASE/.venv"; "$RELEASE/.venv/bin/pip" install -r "$RELEASE/requirements.txt"
REQ_HASH="$(sha256sum "$RELEASE/requirements.txt" | awk '{print $1}')"
"$RELEASE/.venv/bin/python" -c "from release_runtime import ReleaseMetadata,write_release_metadata; write_release_metadata('$RELEASE', ReleaseMetadata.create('$RELEASE_ID','$COMMIT_SHA','$REQ_HASH'))"
RELEASE_ID="$RELEASE_ID" READINESS_PATH="$READY" "$RELEASE/.venv/bin/python" "$RELEASE/main.py" --validate-release
"$RELEASE/.venv/bin/python" -c "from release_runtime import update_release_status; update_release_status('$RELEASE','validated')"
systemctl stop "$SERVICE"; SERVICE_STOPPED=1
MIGRATION_RESULT="$(RELEASE_ID="$RELEASE_ID" READINESS_PATH="$READY" "$RELEASE/.venv/bin/python" "$RELEASE/main.py" --migrate-only)"
[ "${MIGRATION_RESULT#MIGRATIONS_APPLIED=}" != "$MIGRATION_RESULT" ] && [ -n "${MIGRATION_RESULT#MIGRATIONS_APPLIED=}" ] && MIGRATED=1
"$RELEASE/.venv/bin/python" -c "from release_runtime import update_release_status; update_release_status('$RELEASE','migrating', migration_applied=$MIGRATED)"
rm -f "$READY"; ln -s "$RELEASE" "$ROOT/current.next"; mv -Tf "$ROOT/current.next" "$ROOT/current"
systemctl start "$SERVICE"
for _ in $(seq 1 30); do
  [ -f "$READY" ] && grep -q "\"release_id\": \"$RELEASE_ID\"" "$READY" && break
  sleep 1
done
systemctl is-active --quiet "$SERVICE"; grep -q "\"release_id\": \"$RELEASE_ID\"" "$READY"
"$RELEASE/.venv/bin/python" -c "from release_runtime import update_release_status,retain_releases; update_release_status('$RELEASE','ready', migration_applied=$MIGRATED); retain_releases('$ROOT', current_release='$RELEASE_ID')"
