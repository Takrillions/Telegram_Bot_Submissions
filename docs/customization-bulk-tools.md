# Channel Custom Pack bulk tools

Schema v28 adds three owner-only operations for a selected `channel_id`:

- restore the channel's immutable `initial_revision_id`;
- apply the currently active global Standard Custom Pack;
- copy the current **published** Custom Pack from another enabled channel owned by the same Telegram user.

All three operations are draft-first. They never move `channel_custom_state.active_revision_id` directly.

## Flow

```text
source selection
→ read-only diff
→ explicit confirmation
→ persistent channel draft
→ preview
→ explicit publish
→ new immutable revision
```

An existing draft blocks every bulk operation. Owners must publish or discard unfinished work before bulk staging; this prevents an operation from silently overwriting pending edits.

## Reset to initial

`stage_channel_custom_initial_reset()` compares the active revision with the immutable initial channel snapshot. Missing template items are resolved from the Standard revision referenced by the initial snapshot when possible, then from the registry default. Start-card media present in live state but absent from the initial snapshot is staged for deletion.

Publication source:

```text
reset_initial
```

The initial revision itself remains immutable and active history is never rewritten.

## Apply current Standard

`stage_channel_custom_current_standard()` snapshots the **currently active** Standard revision into a draft plan. Existing channels are not automatically synchronized with future Standard changes; applying the current Standard is always an explicit owner action.

Publication source:

```text
apply_current_standard
```

The resulting channel revision records the Standard revision used as provenance.

## Copy from another own channel

Copy is allowed only when the target and source channels are both enabled and:

```text
target.owner_id == source.owner_id == actor Telegram user_id
```

The database revalidates this rule both while staging and again immediately before publication. A forged callback or corrupted draft pointing at another owner's channel therefore fails closed.

Only the source channel's current **published** revision is copied. Its draft is never exposed or copied.

Publication source:

```text
copy_from_channel
```

The source channel is never modified.

## Supported surface

Bulk tools copy only owner-customizable Channel Custom Pack items:

- channel-scoped `template_text` entries;
- `start_card.media` (`photo`, `video`, `animation`).

Global bot/profile templates are ignored. Unknown or legacy changed items are reported as skipped rather than rewritten blindly.

## Audit and provenance

Schema v28 extends `channel_custom_drafts` with nullable provenance fields:

```text
source_channel_id
source_standard_revision_id
```

Staging writes channel-scoped audit events:

```text
initial_reset_staged
current_standard_staged
channel_copy_staged
```

Publication continues to write `draft_published`, including the publication source and source identifiers.

## Isolation guarantees

- no bulk operation writes directly to another channel;
- copy never allows a foreign owner source;
- current Standard application changes only the selected channel draft;
- Standard changes still do not propagate automatically to existing channels;
- live subscriber rendering is unchanged until explicit draft publication.
