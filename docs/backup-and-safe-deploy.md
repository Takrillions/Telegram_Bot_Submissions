# Remote SQLite backups and safe deploy

Production uses two independent restore layers:

1. `DATABASE_BACKUP_DIR` keeps verified local SQLite snapshots.
2. `DATABASE_REMOTE_BACKUP_BUCKET` keeps daily and pre-deploy snapshots in a private Google Cloud Storage bucket outside the VM.

The VM service account must have permission to create/list/delete objects in only that backup bucket. Do not make the bucket public. Configure retention with `DATABASE_REMOTE_BACKUP_KEEP` (default 14 objects) and an optional `DATABASE_REMOTE_BACKUP_PREFIX`.

`telegram-bot-backup.timer` runs a verified remote backup every day at 02:15 UTC and is persistent across reboots. Each snapshot is created with SQLite's backup API, checked with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`, uploaded, and only then rotated.

Every release also stops the bot before migration, creates an unconditional verified pre-deploy snapshot, uploads it remotely, applies migrations, switches `/current`, starts the new release, and waits for its readiness marker. After the verified pre-deploy snapshot exists, any failed release attempt restores that snapshot before switching `/current` back to the previous release and starting the previous service. This is deliberately conservative: migrations commit one-by-one, so a later migration can fail after an earlier one has already committed even when `--migrate-only` exits before reporting applied versions.

Before the first production release-layout transition, create the private GCS bucket, grant the VM service account the minimum object permissions, add `DATABASE_REMOTE_BACKUP_BUCKET` to `shared/.env`, and verify a manual run of `backup_runtime.py --label manual_check --remote-required`. Production migration itself still requires explicit approval.

## Pre-deploy CI gate

Production deployment is blocked by the `verify` job in `.github/workflows/deploy.yml`.
The gate creates a clean Python 3.13 environment on GitHub Actions, installs the real
runtime dependencies from `requirements.txt`, runs `pip check`, compiles all Python
sources, validates `scripts/deploy_release.sh`, and runs the complete unittest suite.
The `deploy` job declares `needs: verify`, so a failed dependency install, import,
compile check, shell syntax check, or regression test prevents any GCP deployment.

The deploy script deliberately treats final `/tmp` artifact cleanup as non-fatal and
uses `sudo rm -f ... || true`; cleanup permission issues must never roll back an
otherwise healthy release.
