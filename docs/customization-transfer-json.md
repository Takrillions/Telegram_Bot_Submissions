# Channel Custom Pack import/export JSON

Stage 10 introduces a versioned JSON transfer format for one channel's **published** customization.

Two different versions are intentionally tracked:

- SQLite/application schema: **v29** (`custom_transfer_json`);
- exported Custom Pack document schema: **v1** (`schema_version: 1`).

The JSON schema version is independent from SQLite migrations and can evolve separately.

## Owner flow

```text
/panel
→ Импорт / экспорт кастома
```

Export:

```text
published immutable revision
→ safe JSON document
→ audit custom_exported
```

Import:

```text
JSON document
→ size / UTF-8 / JSON parse
→ strict versioned schema validation
→ permission validation
→ semantic template validation
→ strict Telegram HTML validation (supported tags, attributes, nesting, entities)
→ Telegram file_id accessibility check
→ read-only diff
→ explicit confirmation
→ persistent draft
→ preview
→ explicit publish
→ new immutable revision (source=import)
```

Import never writes directly to `channel_custom_state.active_revision_id`.

## Export schema v1

Top-level shape:

```json
{
  "format": "telegram_bot_submissions.channel_custom",
  "schema_version": 1,
  "exported_at": "2026-08-17T05:00:00+00:00",
  "source_channel_id": 17,
  "source_channel_title": "Example",
  "source_revision_id": 42,
  "templates": {},
  "display_settings": {
    "labels": {}
  },
  "start_card": {
    "text": "...",
    "media": null
  },
  "metadata": {
    "source_standard_revision_id": 12,
    "template_count": 100,
    "display_label_count": 50,
    "media_portability": "telegram_file_id_must_be_accessible_to_this_bot",
    "omitted_unsupported_items": 0
  }
}
```

`source_channel_id` is informational provenance only. It is never used as the target `channel_id` during import.

## Data deliberately excluded

The transfer schema contains no fields for:

- `owner_id`;
- target `channel_id`;
- `group_id`;
- BOT_TOKEN or other secrets;
- authorization / permissions;
- callback IDs / command routing;
- subscriber IDs;
- messages;
- moderation state;
- statistics;
- private notes / tags;
- GCP credentials;
- database credentials.

Unknown structural fields are rejected instead of ignored. This fail-closed rule prevents a future code path from accidentally accepting a crafted security setting.

## Templates

Only entries already present in `TEMPLATE_REGISTRY` with:

```text
scope == "channel"
```

may be imported.

Global templates are rejected. Unknown template keys are rejected. Every imported text passes the same `validate_template()` semantic validator used by the owner editor.

JSON is an external trust boundary, so imported text also passes a strict validator for Telegram's ordinary `parse_mode=HTML`: only supported formatting tags/attributes are accepted, tags must be correctly nested and closed, unsupported named entities and unescaped `<`, `>` / `&` are rejected, and dynamic template fields are forbidden inside HTML attributes. This prevents a hand-edited import from persisting markup that would later fail when Telegram renders the message.

The Start Card text is stored in `start_card.text` and maps to `start.greeting` internally. UI/display templates are separated into `display_settings.labels`; other channel content templates stay in `templates`.

## Media portability

Raw media bytes are not embedded in v1. Export stores only:

```json
{
  "media_type": "photo | video | animation",
  "telegram_file_id": "..."
}
```

Before an import diff is accepted, the runtime calls Telegram `getFile` for the imported `file_id`. If Telegram says the file is unavailable to this bot, no draft is created. The owner must re-upload media or use an export whose `file_id` is accessible.

This is important because Telegram `file_id` portability is bot/context dependent and should not be assumed across unrelated bots.

## Existing draft protection

A JSON import never merges over an existing persistent customization draft. If a draft already exists, the import is blocked until the owner publishes or discards the existing work.

## Ownership and isolation

Database methods revalidate ownership independently of the inline UI:

```text
channels.owner_id == actor Telegram user_id
```

This check runs for export, import planning and import staging.

The source channel metadata inside JSON has no authority. An imported `source_channel_id` may point to a different or nonexistent channel without changing ownership, permissions or the target channel.

## Audit

Export writes:

```text
custom_exported
```

with active revision, JSON schema version and SHA-256 of the generated file bytes.

Successful import staging writes:

```text
custom_imported
```

with `status=staged`, source metadata, file hash and staged keys. Explicit publication later writes the existing `draft_published` audit event and creates a new revision with:

```text
source = import
```

## Limits

Current v1 import limit:

```text
512 KiB
```

The document must be UTF-8 JSON and declare exactly the supported schema version. A newer or older incompatible `schema_version` produces an explicit compatibility error instead of best-effort parsing.
