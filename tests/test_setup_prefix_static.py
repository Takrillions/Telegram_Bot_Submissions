import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SetupPrefixStaticTests(unittest.TestCase):
    def test_new_setup_waits_for_prefix_before_registration(self):
        source = (ROOT / "handlers.py").read_text(encoding="utf-8")
        setup_start = source.index('@router.message(Command("setup"))')
        setup_end = source.index("# Channel panel and settings", setup_start)
        setup_source = source[setup_start:setup_end]
        self.assertIn("SetupFlow.anonymous_prefix", setup_source)
        self.assertIn('render_default("setup.anonymous_prefix_prompt"', setup_source)
        self.assertIn("anonymous_prefix=prefix", setup_source)
        self.assertIn("Permissions can change while the FSM waits for the prefix", setup_source)

    def test_registration_persists_selected_prefix_only_on_create(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        start = source.index("async def register_channel")
        end = source.index("async def get_channel_by_id", start)
        registration = source[start:end]
        self.assertIn('anonymous_prefix: str = "Анон"', registration)
        self.assertIn("normalized_prefix = self.normalize_anonymous_prefix(anonymous_prefix)", registration)
        self.assertIn("dt_to_db(next_reset), normalized_prefix,", registration)
        existing_branch = registration[: registration.index("count =")]
        self.assertNotIn("anonymous_prefix=?", existing_branch)

    def test_setup_templates_exist_as_global_defaults(self):
        source = (ROOT / "templates.py").read_text(encoding="utf-8")
        self.assertIn('"setup.anonymous_prefix_prompt"', source)
        self.assertIn('"setup.anonymous_prefix_invalid"', source)
        self.assertIn('"Обязательный выбор anonymous prefix при первом /setup', source)


if __name__ == "__main__":
    unittest.main()
