from pathlib import Path
import unittest

from templates import (
    MAX_TEMPLATE_LENGTH,
    TEMPLATE_REGISTRY,
    TemplateValidationError,
    VARIABLE_LABELS,
    friendly_placeholder,
    normalize_editor_template,
    template_field_rows,
    validate_template,
    validation_error_message,
)


class TemplateEditorStage5Tests(unittest.TestCase):
    def test_every_registry_variable_has_human_label(self):
        variables = set()
        for spec in TEMPLATE_REGISTRY.values():
            variables.update(spec.variables)
        self.assertEqual(sorted(variables - set(VARIABLE_LABELS)), [])

    def test_friendly_token_becomes_internal_placeholder_and_keeps_html(self):
        draft = normalize_editor_template(
            "start.greeting",
            "<b>Добро пожаловать в ‹Название предложки›!</b>",
        )
        self.assertEqual(
            draft,
            "<b>Добро пожаловать в {channel_name}!</b>",
        )
        validate_template("start.greeting", draft)

    def test_square_bracket_alias_is_supported(self):
        draft = normalize_editor_template(
            "start.greeting", "Привет, [[Название предложки]]!"
        )
        self.assertEqual(draft, "Привет, {channel_name}!")
        validate_template("start.greeting", draft)

    def test_literal_braces_are_escaped_automatically_for_owner_input(self):
        draft = normalize_editor_template(
            "start.greeting",
            "‹Название предложки›: пример {обычного текста}",
        )
        self.assertEqual(
            draft,
            "{channel_name}: пример {{обычного текста}}",
        )
        validate_template("start.greeting", draft)
        self.assertEqual(
            draft.format(channel_name="A"),
            "A: пример {обычного текста}",
        )

    def test_legacy_supported_placeholder_remains_accepted(self):
        draft = normalize_editor_template(
            "start.greeting", "Привет, {channel_name}!"
        )
        self.assertEqual(draft, "Привет, {channel_name}!")
        validate_template("start.greeting", draft)

    def test_missing_required_field_has_structured_friendly_error(self):
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template("start.greeting", "Привет!")
        self.assertEqual(ctx.exception.code, "missing_required")
        self.assertEqual(ctx.exception.missing, ("channel_name",))
        rendered = validation_error_message(ctx.exception, key="start.greeting")
        self.assertIn("Название предложки", rendered)
        self.assertNotIn("channel_name", rendered)

    def test_too_long_error_reports_actual_length_and_limit(self):
        value = "x" * (MAX_TEMPLATE_LENGTH + 1)
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template("search.empty", value)
        self.assertEqual(ctx.exception.code, "too_long")
        rendered = validation_error_message(ctx.exception, key="search.empty")
        self.assertIn(str(MAX_TEMPLATE_LENGTH + 1), rendered)
        self.assertIn(str(MAX_TEMPLATE_LENGTH), rendered)

    def test_field_rows_expose_friendly_required_flag(self):
        rows = template_field_rows(TEMPLATE_REGISTRY["start.greeting"])
        self.assertEqual(rows, [
            ("channel_name", "Название предложки", friendly_placeholder("channel_name"), True)
        ])

    def test_existing_strict_validator_still_rejects_unknown_internal_field(self):
        with self.assertRaises(ValueError):
            validate_template("start.greeting", "{unknown}")

    def test_handlers_use_native_telegram_formatting_and_friendly_field_ui(self):
        source = Path("handlers.py").read_text(encoding="utf-8")
        self.assertIn("message.html_text", source)
        self.assertIn("normalize_editor_template(key, rich_text)", source)
        self.assertIn('F.data.startswith("template:field:")', source)
        self.assertIn('friendly_placeholder(name)', source)
        self.assertIn('formatted_template_draft(message, key="start.greeting")', source)
        start = source.index('elif data == "panel:start_card:text":')
        end = source.index('elif data == "panel:start_card:media":', start)
        self.assertIn('"start_card.text_prompt"', source[start:end])
        self.assertIn('template_editor_prompt(spec, base)', source[start:end])
        self.assertNotIn('vars=", ".join("{"+item+"}"', source)
        self.assertNotIn('draft = message.text or ""', source)


if __name__ == "__main__":
    unittest.main()
