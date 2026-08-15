import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLERS = (ROOT / "handlers.py").read_text(encoding="utf-8")
TREE = ast.parse(HANDLERS)


class CommandContextStaticTests(unittest.TestCase):
    def test_owner_group_helper_requires_general_context(self):
        fn = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_owner_channel_in_group")
        source = ast.get_source_segment(HANDLERS, fn) or ""
        self.assertIn("if not is_general_forum_message(message)", source)

    def test_setup_rejects_non_general_forum_topics_before_registration(self):
        source = HANDLERS
        handler_pos = source.index("async def setup_handler")
        finish_pos = source.index("await _finish_setup", handler_pos)
        guard_pos = source.index("if not is_general_forum_message(message)", handler_pos)
        self.assertLess(guard_pos, finish_pos)

    def test_topic_admin_commands_resolve_real_topic_mapping(self):
        for handler_name in ("subscriber_history_handler", "subscriber_handler", "status_handler"):
            fn = next(
                node for node in ast.walk(TREE)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == handler_name
            )
            source = ast.get_source_segment(HANDLERS, fn) or ""
            self.assertIn("get_topic_by_group_thread", source, handler_name)


if __name__ == "__main__":
    unittest.main()
