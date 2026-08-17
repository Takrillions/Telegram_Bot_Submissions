from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PredeployGateStaticTests(unittest.TestCase):
    def test_workflow_runs_real_dependency_tests_before_deploy(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        self.assertIn("verify:\n", workflow)
        self.assertIn("uses: actions/setup-python@v7", workflow)
        self.assertIn("python -m pip install -r requirements.txt", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("deploy:\n    needs: verify", workflow)

    def test_deploy_keeps_cleanup_failure_nonfatal(self):
        script = (ROOT / "scripts/deploy_release.sh").read_text(encoding="utf-8")
        self.assertIn('trap - ERR\nsudo rm -f -- "$ARCHIVE" /tmp/deploy_release.sh || true', script)


if __name__ == "__main__":
    unittest.main()
