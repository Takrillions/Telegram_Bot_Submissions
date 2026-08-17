# Telegram command scopes and forum topics

## Product rule

The bot publishes the slash-command suggestion list **only in private chats**.
No command list is published in a group, supergroup, General topic or subscriber
topic. Group commands still work when they are typed manually; handler context
and authorization checks remain the security boundary.

Private command menus are role-aware:

- ordinary user: `/start`, `/channels`, `/privacy`;
- live `CHANNEL_OWNER`: ordinary commands plus `/panel` and `/search`;
- `SUPERADMIN` without a channel: ordinary commands plus `/superadmin`;
- an account that is both `CHANNEL_OWNER` and `SUPERADMIN`: owner commands plus
  `/superadmin`.

`/superadmin` is never published to another user's private command scope. The
handler itself also remains private-only and protected by the numeric
`SUPERADMIN_TELEGRAM_ID`, so command-menu visibility is not used as an
authorization mechanism.

## Why General cannot have its own slash menu

Telegram Bot API command scopes currently support default, all-private, all-
group, all-chat-administrator, chat, chat-administrator and chat-member scopes.
They do **not** accept a forum `message_thread_id`. Consequently, a command list
published for one proposal supergroup cannot be restricted to General; it can
also become visible in subscriber topics of that same supergroup.

The project therefore keeps `GENERAL_OWNER_COMMANDS` and
`TOPIC_ADMIN_COMMANDS` only as manual-command registries. They are not passed
to `setMyCommands`.

## Legacy-scope cleanup

Stopping future group `setMyCommands` calls is not sufficient. Telegram's
command-selection algorithm falls back to broader scopes, including the
default scope. A previous release or manual configuration could therefore
leave group suggestions visible.

Every `sync_command_menus()` run now removes:

- `BotCommandScopeDefault`;
- `BotCommandScopeAllGroupChats`;
- `BotCommandScopeAllChatAdministrators`;
- explicit `BotCommandScopeChat` for every enabled proposal supergroup;
- explicit `BotCommandScopeChatAdministrators` for every enabled proposal
  supergroup.

After cleanup it publishes `BotCommandScopeAllPrivateChats`, then optional
positive-ID private `BotCommandScopeChat` overrides for current owners and the
configured `SUPERADMIN`.

The Bot API does not provide an operation to enumerate arbitrary historical
`BotCommandScopeChatMember` entries that may have been configured externally.
This project has never created such group-member scopes; Stage 12 does not
create them either.

## Manual group command behavior

Owner/system commands are accepted only from General. Subscriber-topic commands
are accepted only in a topic mapped to a subscriber and only from a current
Telegram administrator. Slash-prefixed messages in subscriber topics are kept
on the administrative side and never fall through as subscriber replies.

If Telegram later adds a command scope that can target a forum
`message_thread_id`, the existing `GENERAL_OWNER_COMMANDS` and
`TOPIC_ADMIN_COMMANDS` registries can be used without changing handler
semantics.
