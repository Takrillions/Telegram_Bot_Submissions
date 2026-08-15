#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEPLOY_ROOT:-/home/takrillions/telegram-bot}"
SERVICE="${SERVICE_NAME:-telegram-bot}"
BACKUP_TIMER="${BACKUP_TIMER_NAME:-telegram-bot-backup.timer}"
ARCHIVE="${1:?archive path required}"
RELEASE_ID="${2:?release id required}"
COMMIT_SHA="${3:?commit sha required}"
RELEASES="$ROOT/releases"
SHARED="$ROOT/shared"
RELEASE="$RELEASES/$RELEASE_ID"
export PYTHONPATH="$RELEASE${PYTHONPATH:+:$PYTHONPATH}"
LOCK="$SHARED/deploy.lock"
READY="$SHARED/runtime/readiness.json"
ENV_FILE="$SHARED/.env"
RUNTIME_ENV="$SHARED/runtime/release.env"
LEGACY_UNIT="$SHARED/runtime/telegram-bot.service.legacy"
READINESS_ATTEMPTS=120

mkdir -p "$RELEASES" "$SHARED/runtime" "$SHARED/backups"
exec 9>"$LOCK"
flock -n 9 || { echo "another deployment is active" >&2; exit 1; }

[ -f "$ENV_FILE" ] || { echo "missing shared environment file: $ENV_FILE" >&2; exit 1; }
export ENV_FILE

OLD_CURRENT=""
OLD_RELEASE_ID=""
if [ -L "$ROOT/current" ]; then
  OLD_CURRENT="$(readlink -f "$ROOT/current")"
  OLD_RELEASE_ID="$(basename "$OLD_CURRENT")"
fi

SERVICE_STOPPED=0
CURRENT_SWITCHED=0
MIGRATED=0
DB_ROLLBACK_REQUIRED=0
PREDEPLOY_BACKUP=""

write_runtime_env() {
  local release_id="$1"
  local tmp="$RUNTIME_ENV.tmp"
  umask 077
  {
    printf 'RELEASE_ID=%s\n' "$release_id"
    printf 'READINESS_PATH=%s\n' "$READY"
  } > "$tmp"
  mv -f "$tmp" "$RUNTIME_ENV"
}

restore_previous_release() {
  sudo systemctl stop "$SERVICE" >/dev/null 2>&1 || true

  if [ "$DB_ROLLBACK_REQUIRED" = 1 ] && [ -n "$PREDEPLOY_BACKUP" ] && [ -f "$PREDEPLOY_BACKUP" ]; then
    echo "restoring pre-deploy database snapshot" >&2
    ENV_FILE="$ENV_FILE" "$RELEASE/.venv/bin/python" "$RELEASE/backup_runtime.py" \
      --restore-backup "$PREDEPLOY_BACKUP" || return 1
  fi

  if [ -n "$OLD_CURRENT" ] && [ -d "$OLD_CURRENT" ]; then
    ln -s "$OLD_CURRENT" "$ROOT/current.rollback"
    mv -Tf "$ROOT/current.rollback" "$ROOT/current"
    write_runtime_env "$OLD_RELEASE_ID"
    rm -f "$READY"
    sudo systemctl start "$SERVICE" || return 1
    sudo systemctl is-active --quiet "$SERVICE" || return 1
    echo "previous release restored: $OLD_RELEASE_ID" >&2
    return 0
  fi

  if [ -f "$LEGACY_UNIT" ]; then
    echo "restoring legacy systemd service" >&2
    sudo systemctl disable --now "$BACKUP_TIMER" >/dev/null 2>&1 || true
    rm -f "$ROOT/current" "$READY" "$RUNTIME_ENV"
    sudo install -m 0644 "$LEGACY_UNIT" "/etc/systemd/system/${SERVICE}.service" || return 1
    sudo systemctl daemon-reload || return 1
    sudo systemctl start "$SERVICE" || return 1
    sudo systemctl is-active --quiet "$SERVICE" || return 1
    echo "legacy service restored" >&2
    return 0
  fi

  echo "no previous release or legacy service is available for automatic rollback" >&2
  return 1
}

fail() {
  local status=$?
  trap - ERR
  set +e
  if [ -d "$RELEASE" ] && [ -x "$RELEASE/.venv/bin/python" ]; then
    "$RELEASE/.venv/bin/python" -c \
      "from release_runtime import update_release_status; update_release_status('$RELEASE','failed', migration_applied=$MIGRATED)" \
      >/dev/null 2>&1 || true
  fi

  if [ "$SERVICE_STOPPED" = 1 ] || [ "$CURRENT_SWITCHED" = 1 ]; then
    if ! restore_previous_release; then
      echo "automatic rollback failed; service requires manual recovery" >&2
    fi
  fi
  exit "$status"
}
trap fail ERR

[ ! -e "$RELEASE" ] || { echo "release already exists: $RELEASE" >&2; exit 1; }
mkdir -p "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/pip" install -r "$RELEASE/requirements.txt"
REQ_HASH="$(sha256sum "$RELEASE/requirements.txt" | awk '{print $1}')"
"$RELEASE/.venv/bin/python" -c \
  "from release_runtime import ReleaseMetadata,write_release_metadata; write_release_metadata('$RELEASE', ReleaseMetadata.create('$RELEASE_ID','$COMMIT_SHA','$REQ_HASH'))"

RELEASE_ID="$RELEASE_ID" READINESS_PATH="$READY" ENV_FILE="$ENV_FILE" \
  "$RELEASE/.venv/bin/python" "$RELEASE/main.py" --validate-release
"$RELEASE/.venv/bin/python" -c \
  "from release_runtime import update_release_status; update_release_status('$RELEASE','validated')"

# Install stable units before switching the current symlink. They always execute /current.
sudo install -m 0644 "$RELEASE/deploy/systemd/telegram-bot.service" /etc/systemd/system/telegram-bot.service
sudo install -m 0644 "$RELEASE/deploy/systemd/telegram-bot-backup.service" /etc/systemd/system/telegram-bot-backup.service
sudo install -m 0644 "$RELEASE/deploy/systemd/telegram-bot-backup.timer" /etc/systemd/system/telegram-bot-backup.timer
sudo systemctl daemon-reload

# Stop writes first, then take an unconditional verified pre-deploy snapshot.
sudo systemctl stop "$SERVICE"
SERVICE_STOPPED=1
BACKUP_OUTPUT="$(ENV_FILE="$ENV_FILE" "$RELEASE/.venv/bin/python" "$RELEASE/backup_runtime.py" \
  --label "pre_deploy_${RELEASE_ID}" --remote-required)"
PREDEPLOY_BACKUP="$(printf '%s\n' "$BACKUP_OUTPUT" | sed -n 's/^BACKUP_PATH=//p' | tail -n 1)"
[ -n "$PREDEPLOY_BACKUP" ] && [ -f "$PREDEPLOY_BACKUP" ] || { echo "pre-deploy backup path was not produced" >&2; false; }

# From this point onward rollback must restore the snapshot on *any* failed
# release attempt.  Individual migrations commit atomically one by one, so a
# later migration may fail after an earlier one has already committed.  The
# migrate-only process cannot report that partial success once it exits nonzero.
DB_ROLLBACK_REQUIRED=1
MIGRATION_OUTPUT="$(RELEASE_ID="$RELEASE_ID" READINESS_PATH="$READY" ENV_FILE="$ENV_FILE" \
  "$RELEASE/.venv/bin/python" "$RELEASE/main.py" --migrate-only)"
MIGRATION_LIST="$(printf '%s\n' "$MIGRATION_OUTPUT" | sed -n 's/^MIGRATIONS_APPLIED=//p' | tail -n 1)"
[ -n "$MIGRATION_LIST" ] && MIGRATED=1
"$RELEASE/.venv/bin/python" -c \
  "from release_runtime import update_release_status; update_release_status('$RELEASE','migrating', migration_applied=$MIGRATED)"

rm -f "$READY"
ln -s "$RELEASE" "$ROOT/current.next"
mv -Tf "$ROOT/current.next" "$ROOT/current"
CURRENT_SWITCHED=1
write_runtime_env "$RELEASE_ID"

sudo systemctl start "$SERVICE"
for _ in $(seq 1 "$READINESS_ATTEMPTS"); do
  if [ -f "$READY" ] && grep -Fq "\"release_id\": \"$RELEASE_ID\"" "$READY"; then
    break
  fi
  sleep 1
done
sudo systemctl is-active --quiet "$SERVICE"
grep -Fq "\"release_id\": \"$RELEASE_ID\"" "$READY"

"$RELEASE/.venv/bin/python" -c \
  "from release_runtime import update_release_status,retain_releases; update_release_status('$RELEASE','ready', migration_applied=$MIGRATED); retain_releases('$ROOT', current_release='$RELEASE_ID')"

sudo systemctl enable --now "$BACKUP_TIMER"
trap - ERR
sudo rm -f -- "$ARCHIVE" /tmp/deploy_release.sh || true
printf 'release ready: %s\n' "$RELEASE_ID"
