"""Safe, versioned import/export format for channel customization packs.

The transfer document contains only owner-customizable presentation data.
Ownership, authorization, callbacks, subscriber data and operational records
are deliberately outside the schema and therefore cannot be imported.
"""
from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from templates import TEMPLATE_REGISTRY, validate_template

CUSTOM_PACK_FORMAT = "telegram_bot_submissions.channel_custom"
CUSTOM_PACK_SCHEMA_VERSION = 1
MAX_CUSTOM_PACK_BYTES = 512 * 1024
MAX_MEDIA_FILE_ID_LENGTH = 4096

_ALLOWED_TOP_LEVEL = frozenset({
    "format",
    "schema_version",
    "exported_at",
    "source_channel_id",
    "source_channel_title",
    "source_revision_id",
    "templates",
    "display_settings",
    "start_card",
    "metadata",
})
_ALLOWED_DISPLAY_SETTINGS = frozenset({"labels"})
_ALLOWED_START_CARD = frozenset({"text", "media"})
_ALLOWED_MEDIA = frozenset({"media_type", "telegram_file_id"})
_ALLOWED_METADATA = frozenset({
    "source_standard_revision_id",
    "template_count",
    "display_label_count",
    "media_portability",
    "omitted_unsupported_items",
})


class CustomPackValidationError(ValueError):
    """A transfer document is structurally or semantically unsafe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class NormalizedCustomPack:
    source_channel_id: int | None
    source_channel_title: str
    source_revision_id: int | None
    source_standard_revision_id: int | None
    exported_at: str
    templates: dict[str, str]
    start_card_text: str
    media_type: str | None
    media_file_id: str | None
    document_sha256: str

    def as_target_values(self) -> dict[str, object]:
        """Return only safe values that may be translated into a channel draft."""
        return {
            "templates": dict(self.templates),
            "start_card_text": self.start_card_text,
            "media_type": self.media_type,
            "media_file_id": self.media_file_id,
        }


def _canonical_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def document_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def dumps_export_document(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def build_export_document(
    *,
    channel_id: int,
    channel_title: str,
    revision_id: int,
    source_standard_revision_id: int | None,
    template_texts: Mapping[str, str],
    media: Mapping[str, str] | None,
    omitted_unsupported_items: int = 0,
    exported_at: datetime | None = None,
) -> dict[str, object]:
    """Build schema-v1 export data from an already-authorized channel snapshot."""
    exported_at = exported_at or datetime.now(timezone.utc)
    content_templates: dict[str, str] = {}
    display_labels: dict[str, str] = {}
    start_card_text: str | None = None

    for key, text in sorted(template_texts.items()):
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None or spec.scope != "channel":
            continue
        validate_template(key, text)
        if key == "start.greeting":
            start_card_text = text
        elif key.startswith("ui."):
            display_labels[key] = text
        else:
            content_templates[key] = text

    start_spec = TEMPLATE_REGISTRY.get("start.greeting")
    if start_card_text is None:
        if start_spec is None or start_spec.scope != "channel":
            raise CustomPackValidationError("missing_start_card", "Start Card template is unavailable")
        start_card_text = start_spec.default

    media_block: dict[str, str] | None = None
    if media is not None:
        media_type = media.get("media_type")
        media_file_id = media.get("media_file_id")
        if media_type not in {"photo", "video", "animation"}:
            raise CustomPackValidationError("invalid_media", "Unsupported Start Card media type")
        if not isinstance(media_file_id, str) or not media_file_id or len(media_file_id) > MAX_MEDIA_FILE_ID_LENGTH:
            raise CustomPackValidationError("invalid_media", "Invalid Telegram media file_id")
        media_block = {
            "media_type": media_type,
            "telegram_file_id": media_file_id,
        }

    return {
        "format": CUSTOM_PACK_FORMAT,
        "schema_version": CUSTOM_PACK_SCHEMA_VERSION,
        "exported_at": exported_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "source_channel_id": int(channel_id),
        "source_channel_title": str(channel_title),
        "source_revision_id": int(revision_id),
        "templates": content_templates,
        "display_settings": {"labels": display_labels},
        "start_card": {
            "text": start_card_text,
            "media": media_block,
        },
        "metadata": {
            "source_standard_revision_id": (
                None if source_standard_revision_id is None else int(source_standard_revision_id)
            ),
            "template_count": len(content_templates) + 1,
            "display_label_count": len(display_labels),
            "media_portability": "telegram_file_id_must_be_accessible_to_this_bot",
            "omitted_unsupported_items": max(0, int(omitted_unsupported_items)),
        },
    }


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CustomPackValidationError(
                "duplicate_field", f"JSON содержит повторяющееся поле: {key}."
            )
        result[key] = value
    return result


def parse_import_bytes(raw: bytes) -> tuple[dict[str, object], str]:
    if not isinstance(raw, (bytes, bytearray)):
        raise CustomPackValidationError("invalid_file", "Импорт должен быть JSON-файлом.")
    if not raw:
        raise CustomPackValidationError("empty_file", "JSON-файл пуст.")
    if len(raw) > MAX_CUSTOM_PACK_BYTES:
        raise CustomPackValidationError(
            "file_too_large",
            f"JSON-файл слишком большой. Максимум {MAX_CUSTOM_PACK_BYTES // 1024} КБ.",
        )
    try:
        text = bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CustomPackValidationError("encoding", "JSON должен быть в кодировке UTF-8.") from exc
    try:
        document = json.loads(text, object_pairs_hook=_json_object_without_duplicates)
    except json.JSONDecodeError as exc:
        raise CustomPackValidationError(
            "invalid_json",
            f"Некорректный JSON: строка {exc.lineno}, столбец {exc.colno}.",
        ) from exc
    if not isinstance(document, dict):
        raise CustomPackValidationError("invalid_root", "Корнем JSON должен быть объект.")
    return document, hashlib.sha256(bytes(raw)).hexdigest()


def _require_exact_keys(value: Mapping[str, object], allowed: frozenset[str], *, path: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise CustomPackValidationError(
            "unknown_field",
            f"Недопустимое поле {path}.{extra[0]}. Файл не импортирован.",
        )


def _optional_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CustomPackValidationError("invalid_metadata", f"Поле {field} должно быть положительным целым числом или null.")
    return int(value)


_TELEGRAM_STYLE_TAGS = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler",
})
_TELEGRAM_NON_STYLE_TAGS = frozenset({"a", "tg-emoji", "tg-time", "code", "pre", "blockquote"})
_TELEGRAM_ALLOWED_TAGS = _TELEGRAM_STYLE_TAGS | _TELEGRAM_NON_STYLE_TAGS
_TELEGRAM_NAMED_ENTITIES = frozenset({"lt", "gt", "amp", "quot"})
_TG_TIME_FORMAT_RE = re.compile(r"^(?:r|w?[dD]?[tT]?)$")
_TG_EMOJI_ID_RE = re.compile(r"^[0-9]{1,32}$")
_CODE_LANGUAGE_RE = re.compile(r"^language-[^\\s<>\"']{1,80}$")


class _TelegramHTMLValidator(HTMLParser):
    """Strict validator for the Bot API's ordinary HTML parse mode.

    The normal owner editor stores ``Message.html_text`` generated by Telegram/
    aiogram. JSON import is a separate trust boundary: a hand-edited file must
    not be able to persist malformed or unsupported markup that would later make
    ``sendMessage(parse_mode=HTML)`` fail. The parser therefore accepts only the
    documented basic HTML tags/attributes and verifies balanced/nestable markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def fail(self, message: str) -> None:
        raise CustomPackValidationError("invalid_html", message)

    def _validate_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            self.fail(f"HTML-тег <{tag}> содержит повторяющийся атрибут.")

        def no_attrs() -> None:
            if attrs:
                self.fail(f"HTML-тег <{tag}> не поддерживает атрибуты в Telegram.")

        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href":
                self.fail("HTML-тег <a> должен содержать только атрибут href.")
            href = attrs[0][1]
            if not isinstance(href, str) or not href or len(href) > 4096:
                self.fail("Некорректный href в HTML-ссылке.")
            if any(ch in href for ch in ("\x00", "\r", "\n", "{", "}")):
                self.fail("href содержит недопустимые символы или динамическое поле.")
            return
        if tag == "span":
            if attrs != [("class", "tg-spoiler")]:
                self.fail('Telegram поддерживает <span> только с class="tg-spoiler".')
            return
        if tag == "tg-emoji":
            if len(attrs) != 1 or attrs[0][0] != "emoji-id":
                self.fail("HTML-тег <tg-emoji> должен содержать только emoji-id.")
            value = attrs[0][1]
            if not isinstance(value, str) or not _TG_EMOJI_ID_RE.fullmatch(value):
                self.fail("Некорректный emoji-id в <tg-emoji>.")
            return
        if tag == "tg-time":
            allowed = {"unix", "format"}
            if set(names) - allowed or names.count("unix") != 1 or names.count("format") > 1:
                self.fail("HTML-тег <tg-time> поддерживает только unix и необязательный format.")
            values = dict(attrs)
            unix = values.get("unix")
            if not isinstance(unix, str) or not re.fullmatch(r"-?[0-9]{1,20}", unix):
                self.fail("Некорректный unix timestamp в <tg-time>.")
            fmt = values.get("format")
            if fmt is not None and (not isinstance(fmt, str) or _TG_TIME_FORMAT_RE.fullmatch(fmt) is None):
                self.fail("Некорректный format в <tg-time>.")
            return
        if tag == "blockquote":
            if not attrs:
                return
            if len(attrs) != 1 or attrs[0][0] != "expandable" or attrs[0][1] not in {None, "", "expandable"}:
                self.fail("HTML-тег <blockquote> поддерживает только необязательный атрибут expandable.")
            return
        if tag == "code":
            if not attrs:
                return
            if len(attrs) != 1 or attrs[0][0] != "class":
                self.fail("HTML-тег <code> поддерживает только class=language-* внутри <pre>.")
            value = attrs[0][1]
            if not self.stack or self.stack[-1] != "pre":
                self.fail("class=language-* допустим только у <code> внутри <pre>.")
            if not isinstance(value, str) or _CODE_LANGUAGE_RE.fullmatch(value) is None:
                self.fail("Некорректный class языка в <code>.")
            return
        no_attrs()

    def _validate_nesting(self, tag: str) -> None:
        if self.stack and self.stack[-1] == "code":
            self.fail("Внутри <code> нельзя использовать другие HTML-теги.")
        if self.stack and self.stack[-1] == "pre" and tag != "code":
            self.fail("Внутри <pre> допустим только вложенный <code> языка.")

        if tag in _TELEGRAM_STYLE_TAGS:
            if "pre" in self.stack or "code" in self.stack:
                self.fail(f"HTML-тег <{tag}> нельзя вкладывать в <pre>/<code>.")
            return

        if tag == "pre":
            if self.stack:
                self.fail("HTML-тег <pre> нельзя вкладывать в другие entities.")
            return
        if tag == "code":
            if self.stack == ["pre"]:
                return
            if self.stack:
                self.fail("HTML-тег <code> нельзя вкладывать в другие entities.")
            return

        # Links, custom emoji, date-time and block quotes are non-style
        # entities. Telegram permits style entities around/inside them, but
        # non-style entities must not contain each other; block quotes also
        # cannot be nested.
        if any(open_tag in _TELEGRAM_NON_STYLE_TAGS for open_tag in self.stack):
            self.fail(f"HTML-тег <{tag}> недопустимо вложен в другую Telegram entity.")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _TELEGRAM_ALLOWED_TAGS:
            self.fail(f"HTML-тег <{tag}> не поддерживается обычным Telegram parse_mode=HTML.")
        self._validate_nesting(tag)
        self._validate_attrs(tag, attrs)
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.fail(f"Самозакрывающийся HTML-тег <{tag}/> не поддерживается здесь.")

    def handle_endtag(self, tag: str) -> None:
        if tag not in _TELEGRAM_ALLOWED_TAGS:
            self.fail(f"Закрывающий HTML-тег </{tag}> не поддерживается Telegram.")
        if not self.stack or self.stack[-1] != tag:
            expected = self.stack[-1] if self.stack else None
            if expected is None:
                self.fail(f"Лишний закрывающий HTML-тег </{tag}>.")
            self.fail(f"Нарушена вложенность HTML: ожидался </{expected}>, получен </{tag}>.")
        self.stack.pop()

    def handle_entityref(self, name: str) -> None:
        if name not in _TELEGRAM_NAMED_ENTITIES:
            self.fail(f"HTML entity &{name}; не поддерживается Telegram.")

    def handle_charref(self, name: str) -> None:
        try:
            if name.lower().startswith("x"):
                value = int(name[1:], 16)
            else:
                value = int(name, 10)
        except ValueError:
            self.fail(f"Некорректная числовая HTML entity: &#{name};")
            return
        if value < 0 or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            self.fail(f"Недопустимая Unicode HTML entity: &#{name};")

    def handle_data(self, data: str) -> None:
        # With convert_charrefs=False, a literal ampersand or less-than sign
        # that is not a recognized entity/tag remains data. Telegram requires
        # these characters to be escaped in HTML parse mode.
        if "&" in data or "<" in data or ">" in data:
            self.fail("Символы <, > и & в HTML-тексте должны быть экранированы.")

    def handle_comment(self, data: str) -> None:
        self.fail("HTML-комментарии не поддерживаются Telegram.")

    def handle_decl(self, decl: str) -> None:
        self.fail("HTML declarations не поддерживаются Telegram.")

    def handle_pi(self, data: str) -> None:
        self.fail("HTML processing instructions не поддерживаются Telegram.")

    def close(self) -> None:
        super().close()
        if self.stack:
            self.fail(f"HTML-тег <{self.stack[-1]}> не закрыт.")


def validate_telegram_html(text: str) -> None:
    """Validate imported template markup against ordinary Bot API HTML mode."""
    if not isinstance(text, str):
        raise CustomPackValidationError("invalid_html", "HTML-текст должен быть строкой.")
    parser = _TelegramHTMLValidator()
    try:
        parser.feed(text)
        parser.close()
    except CustomPackValidationError:
        raise
    except Exception as exc:
        raise CustomPackValidationError("invalid_html", "Некорректная HTML-разметка Telegram.") from exc


def normalize_import_document(
    document: Mapping[str, object],
    *,
    raw_sha256: str | None = None,
) -> NormalizedCustomPack:
    """Strictly validate schema and return a security-neutral normalized pack.

    No target channel id, owner id, callbacks, permissions or operational data
    exists in the accepted schema. Unknown fields are rejected rather than
    ignored so a crafted JSON cannot smuggle security settings into future code.
    """
    if not isinstance(document, Mapping):
        raise CustomPackValidationError("invalid_root", "Корнем JSON должен быть объект.")
    _require_exact_keys(document, _ALLOWED_TOP_LEVEL, path="$" )

    if document.get("format") != CUSTOM_PACK_FORMAT:
        raise CustomPackValidationError(
            "wrong_format",
            "Это не файл кастома Telegram Bot Submissions.",
        )
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CustomPackValidationError("invalid_version", "schema_version должен быть целым числом.")
    if version != CUSTOM_PACK_SCHEMA_VERSION:
        raise CustomPackValidationError(
            "unsupported_version",
            f"Версия файла {version} несовместима. Эта сборка поддерживает schema_version={CUSTOM_PACK_SCHEMA_VERSION}.",
        )

    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str) or not exported_at.strip() or len(exported_at) > 80:
        raise CustomPackValidationError("invalid_metadata", "Некорректное поле exported_at.")
    try:
        parsed_dt = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CustomPackValidationError("invalid_metadata", "exported_at должен быть ISO-8601 timestamp.") from exc
    if parsed_dt.tzinfo is None:
        raise CustomPackValidationError("invalid_metadata", "exported_at должен содержать часовой пояс.")

    source_channel_id = _optional_positive_int(document.get("source_channel_id"), field="source_channel_id")
    source_revision_id = _optional_positive_int(document.get("source_revision_id"), field="source_revision_id")
    source_title = document.get("source_channel_title")
    if not isinstance(source_title, str) or len(source_title) > 255:
        raise CustomPackValidationError("invalid_metadata", "source_channel_title должен быть строкой длиной до 255 символов.")

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CustomPackValidationError("invalid_metadata", "metadata должен быть объектом.")
    _require_exact_keys(metadata, _ALLOWED_METADATA, path="$.metadata")
    source_standard_revision_id = _optional_positive_int(
        metadata.get("source_standard_revision_id"), field="metadata.source_standard_revision_id"
    )
    for count_key in ("template_count", "display_label_count", "omitted_unsupported_items"):
        count = metadata.get(count_key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CustomPackValidationError("invalid_metadata", f"metadata.{count_key} должно быть неотрицательным целым числом.")
    portability = metadata.get("media_portability")
    if portability != "telegram_file_id_must_be_accessible_to_this_bot":
        raise CustomPackValidationError("invalid_metadata", "Неизвестное правило переносимости media.")

    templates = document.get("templates")
    if not isinstance(templates, Mapping):
        raise CustomPackValidationError("invalid_templates", "templates должен быть объектом.")
    display = document.get("display_settings")
    if not isinstance(display, Mapping):
        raise CustomPackValidationError("invalid_display", "display_settings должен быть объектом.")
    _require_exact_keys(display, _ALLOWED_DISPLAY_SETTINGS, path="$.display_settings")
    labels = display.get("labels")
    if not isinstance(labels, Mapping):
        raise CustomPackValidationError("invalid_display", "display_settings.labels должен быть объектом.")

    start_card = document.get("start_card")
    if not isinstance(start_card, Mapping):
        raise CustomPackValidationError("invalid_start_card", "start_card должен быть объектом.")
    _require_exact_keys(start_card, _ALLOWED_START_CARD, path="$.start_card")
    start_text = start_card.get("text")
    if not isinstance(start_text, str):
        raise CustomPackValidationError("invalid_start_card", "start_card.text должен быть строкой.")

    normalized_templates: dict[str, str] = {}

    def add_template(key: object, value: object, *, expect_ui: bool) -> None:
        if not isinstance(key, str) or not isinstance(value, str):
            raise CustomPackValidationError("invalid_templates", "Ключи и значения шаблонов должны быть строками.")
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None:
            raise CustomPackValidationError("unknown_template", f"Неизвестный шаблон: {key}.")
        if spec.scope != "channel":
            raise CustomPackValidationError("global_template", f"Глобальный шаблон {key} нельзя импортировать в предложку.")
        if key == "start.greeting":
            raise CustomPackValidationError("duplicate_start_card", "start.greeting должен находиться только в start_card.text.")
        if expect_ui and not key.startswith("ui."):
            raise CustomPackValidationError("invalid_display", f"{key} не является display/UI шаблоном.")
        if not expect_ui and key.startswith("ui."):
            raise CustomPackValidationError("invalid_templates", f"UI-шаблон {key} должен находиться в display_settings.labels.")
        try:
            validate_template(key, value)
            validate_telegram_html(value)
        except CustomPackValidationError as exc:
            raise CustomPackValidationError(exc.code, f"Шаблон {key}: {exc.message}") from exc
        except ValueError as exc:
            raise CustomPackValidationError("invalid_template_text", f"Шаблон {key}: {exc}") from exc
        normalized_templates[key] = value

    for key, value in templates.items():
        add_template(key, value, expect_ui=False)
    for key, value in labels.items():
        add_template(key, value, expect_ui=True)
    try:
        validate_template("start.greeting", start_text)
        validate_telegram_html(start_text)
    except CustomPackValidationError as exc:
        raise CustomPackValidationError(exc.code, f"Стартовая карточка: {exc.message}") from exc
    except ValueError as exc:
        raise CustomPackValidationError("invalid_template_text", f"Стартовая карточка: {exc}") from exc
    normalized_templates["start.greeting"] = start_text

    media = start_card.get("media")
    media_type: str | None = None
    media_file_id: str | None = None
    if media is not None:
        if not isinstance(media, Mapping):
            raise CustomPackValidationError("invalid_media", "start_card.media должен быть объектом или null.")
        _require_exact_keys(media, _ALLOWED_MEDIA, path="$.start_card.media")
        media_type_value = media.get("media_type")
        file_id_value = media.get("telegram_file_id")
        if media_type_value not in {"photo", "video", "animation"}:
            raise CustomPackValidationError("invalid_media", "Недопустимый тип media в стартовой карточке.")
        if not isinstance(file_id_value, str) or not file_id_value or len(file_id_value) > MAX_MEDIA_FILE_ID_LENGTH:
            raise CustomPackValidationError("invalid_media", "Некорректный Telegram file_id в стартовой карточке.")
        media_type = str(media_type_value)
        media_file_id = file_id_value

    expected_template_count = int(metadata["template_count"])
    expected_display_count = int(metadata["display_label_count"])
    actual_display_count = sum(1 for key in normalized_templates if key.startswith("ui."))
    actual_content_count = len(normalized_templates) - actual_display_count
    if expected_template_count != actual_content_count or expected_display_count != actual_display_count:
        raise CustomPackValidationError(
            "count_mismatch",
            "Счётчики шаблонов в metadata не совпадают с содержимым файла.",
        )

    digest = raw_sha256 or document_sha256(document)
    return NormalizedCustomPack(
        source_channel_id=source_channel_id,
        source_channel_title=source_title,
        source_revision_id=source_revision_id,
        source_standard_revision_id=source_standard_revision_id,
        exported_at=exported_at,
        templates=normalized_templates,
        start_card_text=start_text,
        media_type=media_type,
        media_file_id=media_file_id,
        document_sha256=digest,
    )


def parse_and_normalize_import(raw: bytes) -> NormalizedCustomPack:
    document, digest = parse_import_bytes(raw)
    return normalize_import_document(document, raw_sha256=digest)
