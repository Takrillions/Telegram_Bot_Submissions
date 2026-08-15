import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CleanupStaticTests(unittest.TestCase):
    def test_python_modules_have_no_duplicate_top_level_definitions(self):
        for path in ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            seen: dict[str, int] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.assertNotIn(node.name, seen, f"duplicate {node.name} in {path.name}:{node.lineno}")
                    seen[node.name] = node.lineno

    def test_readme_describes_current_channel_model(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("channel_id", readme)
        self.assertIn("ref_c_CHANNEL_ID", readme)
        self.assertIn("anonymous", readme)
        self.assertIn("массовая рассылка", readme.lower())
        self.assertNotIn("owner_id <-> admin_group_id", readme)
        self.assertNotIn("tenant_subscribers", readme)

    def test_admin_guard_has_no_dead_ttl_state(self):
        source = (ROOT / "handlers.py").read_text(encoding="utf-8")
        self.assertNotIn("self.ttl_seconds", source)
