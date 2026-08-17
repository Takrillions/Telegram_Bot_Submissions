# Customization architecture foundation

Schema version 23 introduced the storage foundation for the customization architecture. Stages 4–6 progressively activated that foundation. Schema v26 / Stage 7 completes the cutover to persistent channel drafts and atomic publication; v27 exposes immutable history and rollback-to-draft; v28 adds reset/apply/copy bulk tools with draft provenance; v29 adds strict versioned JSON import/export that remains draft-first; v30 / Stage 11 completes the separation between the global Telegram bot profile and the channel-only Standard Custom Pack. The legacy override table remains only for migration/backwards-compatibility purposes.

## Standard Custom Pack

`bot_standard_custom_revisions` and `bot_standard_custom_items` store immutable global standard snapshots. `bot_standard_custom_state` points to the currently active standard revision. Migration 23 seeds revision 1 from the current application template registry.

The active standard revision is now copied into every **new** channel during `/setup`. Updating the standard must never silently rewrite existing channel custom packs.

## Channel Custom Pack

Every channel that exists when migration 23 runs receives one immutable `migration_snapshot` revision containing its then-current effective template text: a legacy override when one exists, otherwise the application default. Unknown/stale legacy override keys are also retained so the migration never silently destroys customization data. Every channel created after v23 is active receives an immutable `setup_snapshot` copied byte-for-byte from the Standard Custom Pack revision that is active at that moment.

`channel_custom_state` stores the active and initial revision plus the standard revision from which the channel foundation originated. Composite foreign keys bind active/initial revisions to the same `channel_id`, preventing a channel state row from pointing at another channel's revision even if application code is wrong.

## Atomic `/setup` snapshot

New-channel registration is one SQLite write transaction guarded by `BEGIN IMMEDIATE`. The transaction contains:

1. ownership/channel-limit checks;
2. the `channels` row;
3. the anonymous counter row;
4. a `channel_custom_revisions` row with source `setup_snapshot`;
5. all items copied from the active Standard Custom Pack;
6. `channel_custom_state`;
7. a `customization_audit_log` event.

Only after all of these writes succeed does registration commit. If the active standard state/revision/items are missing, or any snapshot write fails, the transaction rolls back and no half-configured channel remains. Re-running `/setup` for an existing channel updates the group title/enabled state but deliberately does **not** replace its initial Custom Pack snapshot.

Current code still supports migration tests that intentionally open pre-v23 schemas: when migration 23 is not recorded, legacy registration omits the new snapshot. On a database that records v23, missing customization structures are treated as corruption and setup fails closed.

## Live render stack after Stage 7

Normal subscriber/admin runtime resolves channel text as:

```text
1. immutable active Channel Custom Pack revision
2. application registry default (last-resort fallback for newly introduced keys)
```

Owner preview surfaces explicitly opt into a third overlay:

```text
channel draft item → published revision → registry fallback
```

The important invariant is that unpublished draft data is never used by normal message routing. `channel_template_overrides` is folded into a successor immutable revision during migration v26 and then cleared; it remains only as compatibility storage/API for old fixtures and migration history, not as the current owner editor.

Specs marked `scope="global"` bypass channel revisions/drafts entirely. In particular all `prestart.*` specs are global and cannot be exposed through the CHANNEL_OWNER template editor.

## Audit foundation

`customization_audit_log` is created in version 23. Migration-created events use `actor_user_id = NULL` rather than attributing automated migration work to a human administrator.

## Security invariants

- standard state is global singleton state;
- channel revision state is always scoped by `channel_id`;
- composite foreign keys reject cross-channel active/initial revisions;
- migration 23 does not alter channel ownership, subscribers, topics, privacy, sanctions, statistics, broadcasts, reactions, cleanup or the global Telegram pre-Start profile.


## Schema v24: channel start-card media

Version 24 adds `channel_start_card_media`, one optional row per `channel_id`. The row stores only Telegram media type (`photo`, `video`, or `animation`), the bot-specific `file_id`, and update metadata. Its foreign key cascades with the channel, so media cannot survive as an orphan and no global singleton is involved.

The post-Start card is deliberately different from Telegram's global pre-Start profile. Its text is the existing channel-scoped `start.greeting` template; its media comes from `channel_start_card_media`. A `ref_c_CHANNEL_ID` deep-link therefore renders exactly that channel's media and greeting. If Telegram rejects a stale media `file_id`, the runtime logs the failure and still sends the text, so media cannot block onboarding.

The owner panel exposes `Стартовая карточка` for every `CHANNEL_OWNER`. Global bot-profile editing is not part of the channel panel: Stage 11 moves it to the independent SUPERADMIN-only `/superadmin` surface. Since Stage 7, start-card text/media edits are staged in the same channel draft as other templates and become live only after explicit atomic publication.

## Stage 5 — human-friendly rich-text editor

Channel owners no longer need to type Telegram HTML or internal Python-style
placeholders in the normal editor.

The editor now accepts an ordinary formatted Telegram message. aiogram's
`Message.html_text` is used as the safe internal representation, so formatting
chosen in Telegram (bold, italic, links and other supported entities) is
preserved while raw `<tag>` text typed by the owner is escaped rather than
executed as markup.

Dynamic values are shown with Russian human-readable markers such as:

```text
‹Название предложки›
‹Причина›
‹Срок›
```

The normal UI does not require the owner to know names such as
`{channel_name}`. Exact legacy placeholders remain accepted internally for
backwards compatibility with existing saved overrides and power-user input.

Required fields are explicitly marked in the editor. If a required field is
missing, the owner receives a concrete error naming the missing human-readable
field. Length and structural validation errors are likewise reported with a
specific reason instead of the old generic "проверьте длину, переменные и
формат" message.

Literal braces in new owner input are escaped automatically by the editor, so
ordinary prose containing `{...}` does not require knowledge of Python
`Formatter` escaping rules. The low-level validator remains strict for stored
or legacy values and continues to reject unsupported internal placeholders.

Stage 5 changes only the editing UX and internal normalization. It does not yet
introduce persistent drafts, atomic multi-change publication or revision
history; those belong to later stages in the customization plan.

## Schema v25 / Stage 6 — channel template surface

Stage 6 moves the main safe human-readable UI/reply surface into channel-scoped templates. This includes panel labels, cleanup labels, reaction controls, channel start-card controls, broadcast/search/status/subscriber labels, privacy labels, moderation display labels, statistics bodies, subscriber statistics/history structures and metadata controls. Protocol identifiers remain immutable: callback data, commands, status keys, enum values, permission keys and migration identifiers are never owner-customizable.

The CHANNEL_OWNER editor now enumerates only `scope="channel"` specs. Global bot/pre-Start specs are hidden and ignored by channel overrides even if stale rows exist. Customizable inline-button labels are normalized through `render_label()`, which strips HTML/newlines and bounds the result to Telegram-safe button text while leaving callback identifiers unchanged.

Migration 25 does not update any `channel_custom_*` rows. If new registry items are absent from the active Standard Pack, it creates an immutable successor Standard revision, copies all previous Standard items, appends only missing defaults, advances `bot_standard_custom_state`, and records a system audit event. Existing channels remain on their existing snapshot revisions.

Stage 6 originally kept immediate writes for compatibility. Schema v26 / Stage 7 below replaces that final mutable overlay.
## Schema v26 / Stage 7 — persistent drafts and atomic publish

Version 26 adds `channel_custom_drafts` and `channel_custom_draft_items`. A draft is bound to the exact `base_revision_id` that was live when editing started. Any later change of the active revision causes publish to fail closed with a draft conflict instead of silently overwriting newer data.

Owner edits to channel templates, start-card media and template reset actions no longer mutate the live configuration. They are stored as draft item operations (`set` / `delete`). Normal `render_template()` calls keep `include_draft=False`; only owner preview flows explicitly request the overlay.

`publish_channel_custom_draft()` executes under the database write lock with `BEGIN IMMEDIATE`. It verifies the base revision, applies every draft item to one full immutable snapshot, inserts one `manual_publish` revision, advances `channel_custom_state.active_revision_id`, synchronizes the legacy start-card-media compatibility table, records `draft_published` in the audit log and deletes the draft. The commit happens only after every step succeeds. Any exception rolls the transaction back, leaving both the previous live revision and the draft intact.

Draft discard deletes only unpublished state and records `draft_discarded`; it never changes the active revision.

Migration v26 also freezes any pre-existing Stage-6 `channel_template_overrides` and `channel_start_card_media` into a successor immutable live revision before clearing the override table. Thus the upgrade preserves the exact user-visible state at the cutover point.

Revision browsing/rollback UI is intentionally deferred to Stage 8; Stage 7 already creates the immutable publication revisions and audit records that Stage 8 will expose.


## Schema v27 / Stage 8 — revision history, audit and rollback-to-draft

Version 27 exposes the immutable revision history created by the previous stages without introducing destructive rollback. The owner panel now contains `История изменений`, with paginated Channel Custom Pack revisions, revision metadata/diff, a historical Channel Start Card preview, and a channel-scoped audit browser.

Rollback is deliberately two-step. Selecting an old revision never changes `channel_custom_state.active_revision_id`. Instead `stage_channel_custom_revision_restore()` compares the selected historical snapshot with the current live revision and writes only supported differences into a **fresh draft**. An existing non-empty draft blocks restore so unpublished owner work cannot be overwritten silently. The restored draft can be previewed, discarded, or explicitly published.

Schema v27 adds publication intent metadata to `channel_custom_drafts`:

```text
publish_source
publish_summary
restore_revision_id
```

Normal editing keeps `publish_source = manual_publish`. A history restore marks the draft as `rollback`; explicit publication then creates a **new immutable revision** with source `rollback`. Historical rows are never edited or deleted. The audit log records both `revision_restore_staged` and the later `draft_published` event.

Historical restore remains channel-isolated. A revision must belong to the same `channel_id`; a revision ID from another channel is rejected. Unsupported/unknown legacy custom items are never blindly rewritten. For older revisions missing a newer template key, restore uses the Standard revision referenced by the historical snapshot when possible, then falls back to the current registry default.

Stage 8 also starts auditing SUPERADMIN changes to the real global pre-Start profile (`global_profile` scope): description updates, prepared media updates/removal, and reset operations record the real actor ID. This does not make global profile state channel-scoped.

Migration v27 does **not** rewrite existing Channel Custom Pack revisions. It only adds rollback-intent columns to the draft table and appends newly introduced history/audit UI defaults to a successor Standard revision for future channels.

## Schema v28 / Stage 9 — reset, current Standard and own-channel copy

Version 28 adds owner-facing bulk tools without changing the draft-first safety model. The selected channel can be compared against and staged from three sources: its immutable initial revision, the currently active Standard Custom Pack, or another enabled channel owned by the same Telegram user.

Every operation follows `read-only diff → confirmation → draft → preview → explicit publish`. Existing drafts block bulk staging. No source is applied directly to `active_revision_id`.

Draft provenance adds nullable `source_channel_id` and `source_standard_revision_id`. Publication sources are `reset_initial`, `apply_current_standard`, and `copy_from_channel`; the resulting immutable revision retains the effective Standard revision provenance. Copy authorization is revalidated in the database both at staging and immediately before publication so forged callbacks cannot copy a foreign owner's channel.

Only channel-scoped template text and `start_card.media` participate in bulk operations. Global bot/profile specs are ignored, while unknown legacy differences are skipped rather than rewritten. Standard changes still never propagate automatically to existing channels.



## Schema v29 / Stage 10 — safe versioned JSON transfer

Version 29 adds an owner-only `Импорт / экспорт кастома` surface for the selected channel. The SQLite schema itself needs no new mutable customization table; existing immutable revisions, persistent drafts and audit rows are reused. Migration v29 only appends newly introduced transfer UI defaults to a successor Standard revision when they are missing, without rewriting existing channel snapshots.

Export reads only the current **published** immutable channel revision. Unpublished draft values are deliberately excluded. The generated document uses an independent transfer schema (`schema_version = 1`) and contains channel-scoped templates, UI/display labels, Start Card text, optional Telegram media metadata and non-secret source provenance. It never queries or serializes subscriber, moderation, message, statistics, private-note, authorization or credential data.

Import is fail-closed: unknown structural fields, incompatible schema versions, global/unknown templates, invalid template semantics and malformed media are rejected. The JSON has no target `channel_id`, `owner_id`, permission or callback-routing field. `source_channel_id` is informational provenance only and has no authority over the destination. Database methods independently require `channels.owner_id == actor_user_id` for export, import planning and import staging.

If media is present, the runtime verifies the imported Telegram `file_id` with `getFile` before showing the import diff. An inaccessible `file_id` aborts the operation without creating a draft.

The import lifecycle remains:

```text
validated JSON → read-only diff → explicit confirmation → draft → preview → explicit publish
```

An existing draft blocks import so pending work is never overwritten. Staging records `custom_imported`; export records `custom_exported`. Final publication creates a new immutable revision with `source = import` and the normal `draft_published` audit event.

See `docs/customization-transfer-json.md` for the transfer schema and security boundary.


## Schema v30 / Stage 11 — SUPERADMIN Standard editor and Global Bot Profile separation

Stage 11 makes the global security boundary explicit. `/superadmin` is a private-chat-only control surface authorized solely by the configured numeric `SUPERADMIN_TELEGRAM_ID`. It does not require the actor to own any channel and it does not grant ownership or CHANNEL_OWNER permissions for other channels. A missing or invalid SUPERADMIN configuration remains fail-closed for every global action.

The SUPERADMIN surface deliberately contains two separate products:

1. **Global Bot Profile** — one Telegram-level profile for the entire bot. Description can be applied through the Bot API. Description Picture is stored only as a candidate and the UI explicitly instructs SUPERADMIN to apply the actual picture through `@BotFather`; avatar/name/About/Bio and other BotFather-managed profile fields are never presented as channel-specific settings. Global-profile changes use the `global_profile` audit scope.
2. **Standard Custom Pack** — the immutable default channel customization copied only when a new proposal is registered with `/setup`. The editor exposes only `scope="channel"` templates plus optional `start_card.media`. Each effective change creates a new immutable Standard revision with the real SUPERADMIN actor in audit/history. Existing channels do not auto-inherit the new Standard revision.

Migration v30 creates a successor Standard revision that removes every non-channel template item from the **active** Standard Pack while retaining all channel-scoped items and optional `start_card.media`. It does not rewrite or repoint any existing `channel_custom_state`, Channel Custom Pack revision, initial snapshot, draft, or historical Standard revision. Thus a pre-v30 channel snapshot remains byte-for-byte/logically unchanged even if it historically contains now-global template items.

The former direct global-profile entry in the CHANNEL_OWNER panel is removed. A user who is both CHANNEL_OWNER and SUPERADMIN sees only a navigation entry to `Глобальное управление ботом`; ordinary CHANNEL_OWNER accounts see no global controls at all. Stale pre-Stage-11 `panel:prestart*` callbacks are tombstoned: non-SUPERADMIN actors are denied, and SUPERADMIN is redirected to `/superadmin` rather than executing the old channel-coupled flow.

Standard template editing uses the same human-friendly Telegram rich-text input and dynamic-field vocabulary as channel editing, but publication is intentionally immediate at the **Standard revision** level after an explicit confirmation. This is safe because Standard revisions are not live overlays for existing channels: only future `/setup` snapshots consume the newly active revision. Standard Start Card media follows the same immutable revision model.

Global audit browsing combines `global_standard` and `global_profile` events while preserving their separate scope labels. Standard revision history is independently paginated from Channel Custom Pack history.
