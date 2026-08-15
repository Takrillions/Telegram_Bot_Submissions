from pathlib import Path
import ast
import unittest


class TextIntegrityTests(unittest.TestCase):
    CORE_FILES = (
        "handlers.py",
        "templates.py",
        "database.py",
        "scheduler.py",
        "authorization.py",
        "command_menu.py",
        "export_runtime.py",
        "reaction_runtime.py",
        "broadcast_runtime.py",
        "backup_runtime.py",
        "prestart_card.py",
        "release_runtime.py",
        "main.py",
    )
    MOJIBAKE_MARKERS = (
        "РќР", "РµР", "Р°Р", "Р»Р", "РёР", "РЅР", "СЃР", "С‚Р",
        "РЎР", "РђР", "Р’Р", "РљР", "РњР", "РџР", "РћР", "СЂР",
    )

    def test_core_sources_have_no_known_encoding_damage(self):
        problems = []
        for file_name in self.CORE_FILES:
            text = Path(file_name).read_text(encoding="utf-8")
            if "�" in text:
                problems.append(f"{file_name}: replacement character")
            if "??" in text:
                problems.append(f"{file_name}: repeated question marks")
            for marker in self.MOJIBAKE_MARKERS:
                if marker in text:
                    problems.append(f"{file_name}: mojibake marker {marker!r}")
                    break
        self.assertEqual(problems, [])


    def test_no_new_direct_full_text_telegram_sends(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        telegram_methods = {
            "answer", "edit_text", "send_message", "send_document", "send_photo",
            "send_video", "send_animation", "send_audio", "send_voice",
        }
        allowed_fragments = (
            "Предпросмотр",  # structural wrapper around a rendered template/draft
            "confirmation",  # composition of two already rendered moderation templates
        )
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in telegram_methods:
                continue
            text_nodes = []
            if node.args:
                text_nodes.append(node.args[0])
            for keyword in node.keywords:
                if keyword.arg in {"text", "caption"}:
                    text_nodes.append(keyword.value)
            for text_node in text_nodes:
                if not isinstance(text_node, (ast.Constant, ast.JoinedStr)):
                    continue
                if isinstance(text_node, ast.Constant) and not isinstance(text_node.value, str):
                    continue
                snippet = ast.get_source_segment(source, text_node) or ""
                if any(fragment in snippet for fragment in allowed_fragments):
                    continue
                offenders.append(f"line {node.lineno}: {snippet[:120]}")
        self.assertEqual(offenders, [])

    def test_template_registry_metadata_is_not_corrupted(self):
        # Keep this check dependency-free so encoding regressions are caught even
        # before the Telegram/database stack is imported.
        text = Path("templates.py").read_text(encoding="utf-8")
        for key in (
            "sanction.flow.invalid_callback",
            "subscriber.metadata.note_prompt",
            "subscriber.metadata.tags_title",
        ):
            self.assertIn(key, text)
        self.assertIn("Ограничения и модерация", text)
        self.assertIn("Подписчица", text)
        self.assertNotIn("_FLOW_DEFAULTS", text)


if __name__ == "__main__":
    unittest.main()
