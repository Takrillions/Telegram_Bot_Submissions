# Telegram command scopes and forum topics

The command registry contains three levels: ordinary private-user commands,
administrative commands for subscriber topics, and owner/system commands for
the General topic or private owner panel.

Telegram Bot API command scopes are limited to global, chat, administrator and
chat-member scopes.  They cannot target a forum `message_thread_id`; therefore
they cannot show a command menu in General while hiding it in other topics of
the same supergroup.  Publishing a group menu would violate the requirement
that subscriber topics remain uncluttered, so this project intentionally
publishes private-chat menus only.

Commands in a group still work when typed manually. Server-side handlers
enforce their context: owner/system commands are accepted only from General,
including the case where a reply inside General carries a generic thread id;
all actual forum topics are rejected. Subscriber-topic commands are permitted
only in a topic that is mapped to a subscriber conversation and only for a
current Telegram administrator. Any slash-prefixed message in a subscriber
topic is kept on the administrative side and never falls through as a reply to
the subscriber. If Telegram introduces topic-level command scopes,
`GENERAL_OWNER_COMMANDS` and `TOPIC_ADMIN_COMMANDS` are the single registry
ready to use them.
