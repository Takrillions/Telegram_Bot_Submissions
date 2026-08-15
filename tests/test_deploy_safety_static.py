import unittest
from pathlib import Path


class DeploySafetyStaticTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.script = (self.root / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")

    def test_deploy_takes_remote_predeploy_backup_before_migration(self):
        backup = self.script.index("--label \"pre_deploy_${RELEASE_ID}\" --remote-required")
        migration = self.script.index("--migrate-only")
        self.assertLess(backup, migration)

    def test_failed_migration_path_can_restore_snapshot_and_previous_release(self):
        self.assertIn("--restore-backup", self.script)
        self.assertIn("restore_previous_release", self.script)
        self.assertIn("mv -Tf \"$ROOT/current.rollback\" \"$ROOT/current\"", self.script)

    def test_partial_migration_failure_restores_predeploy_snapshot(self):
        backup_ready = self.script.index('DB_ROLLBACK_REQUIRED=1')
        migration = self.script.index('MIGRATION_OUTPUT="$(')
        self.assertLess(backup_ready, migration)
        self.assertIn('if [ "$DB_ROLLBACK_REQUIRED" = 1 ]', self.script)
        self.assertNotIn('if [ "$MIGRATED" = 1 ] && [ -n "$PREDEPLOY_BACKUP" ]', self.script)

    def test_deploy_uses_shared_environment_and_readiness(self):
        self.assertIn('ENV_FILE="$SHARED/.env"', self.script)
        self.assertIn('READY="$SHARED/runtime/readiness.json"', self.script)
        self.assertIn("systemctl is-active --quiet", self.script)

    def test_daily_backup_timer_is_installed_and_enabled(self):
        self.assertIn("telegram-bot-backup.timer", self.script)
        self.assertIn('systemctl enable --now "$BACKUP_TIMER"', self.script)


if __name__ == "__main__":
    unittest.main()
