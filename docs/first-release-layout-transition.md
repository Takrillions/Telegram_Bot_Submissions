# First release-layout transition

Do not run this procedure without explicit production approval.

1. Read the actual `telegram-bot.service` and determine the effective database path without printing secrets.
2. Preserve `/home/takrillions/Telegram_Bot_Submissions-main` as the legacy fallback.
3. Create `/home/takrillions/telegram-bot/{releases,shared/data,shared/backups,shared/runtime}`.
4. Stop the legacy service and create a SQLite-aware verified snapshot before moving the live database into `shared/data`.
5. Copy the real `.env` to `shared/.env` with mode `0600`, then use absolute `DATABASE_PATH` and `DATABASE_BACKUP_DIR`.
6. Create a private Google Cloud Storage bucket outside the VM, grant the VM service account minimum object permissions, and configure `DATABASE_REMOTE_BACKUP_BUCKET`, prefix, and retention in `shared/.env`.
7. Install the repository systemd units from `deploy/systemd/`. The application unit always executes `/current`; the backup timer runs a verified remote SQLite backup daily.
8. Run a manual `backup_runtime.py --label manual_check --remote-required` and verify the object exists before enabling automatic deploys.
9. Only then run the first release deployment. The deploy script takes another verified pre-deploy backup after stopping writes, migrates, switches `/current`, waits for readiness, and automatically restores the previous release/database snapshot if startup fails.

If the legacy-to-release transition itself fails before the first `/current` release exists, restore the preserved legacy unit and service manually.
