# Administrator reaction routing

Reaction handling is scoped by `channel_id`. The default mode notifies the subscriber when a current group administrator adds a reaction to a subscriber-originated forum message. Removing a reaction does nothing.

Mode 2 routes a reacted subscriber message to one dedicated service topic. Identified messages prefer Telegram forwarding with a copy fallback. Anonymous messages are always copied and are labelled only with the current anonymous tag. The subscriber receives no service-routing notification.

The bot stores the destination forum message ID when it copies each subscriber message into a user topic. Reaction updates are accepted only for those recorded messages, which automatically excludes General, bot cards, administrator replies and the reaction service topic.

Mode configuration is owner-only and must be performed from General. The first activation of mode 2 creates a service topic. The owner can rename or recreate it. If Telegram reports that the service topic is missing or closed, the setting is marked as requiring repair and no alternative topic is selected automatically.

Reaction events are deduplicated. In mode 2, the dispatch journal has one record per source forum message, so multiple administrators reacting to the same source cannot create duplicate routed copies.
