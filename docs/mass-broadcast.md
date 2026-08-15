# Mass broadcast

`/broadcast` is accepted only in General of a configured forum supergroup and
only from the stored channel owner who is still a current Telegram admin.
Group command menus remain hidden because Bot API command scopes cannot target
General separately.

A draft stores the exact source Telegram message from General. Telegram media
groups are buffered briefly, persisted as one ordered source-message set and
previewed/sent with `copyMessages`, which preserves album grouping. The owner
must explicitly press **Отправить** after preview.

When sending starts, the database snapshots unique subscriber user IDs. Each
recipient is journaled by `(broadcast_id, channel_id, user_id)`. Immediately
before delivery the bot resolves that user's current privacy mode and current
topic; it never falls back to a topic from the other privacy mode. Closed,
missing or deleted current topics are undelivered.

Delivery is deliberately at-most-once. A row is moved to `reserved` before the
Telegram copy call. If the process crashes in that narrow window, resume skips
that recipient rather than risking a duplicate. Other still-pending recipients
can be resumed from General with `/broadcast`.
