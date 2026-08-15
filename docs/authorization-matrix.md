# Channel-scoped roles

`channels.owner_id` is the sole source of truth for the main administrator of
that channel.  It is assigned by the first successful `/setup` and is never
changed by a callback or a repeated setup.  A group administrator is not made
an owner merely by their Telegram status.

| Action family | Current Telegram group administrator | Channel owner |
| --- | --- | --- |
| Work in subscriber topics, replies | yes | yes |
| Subscriber moderation, notes, tags, history | yes | yes |
| `/panel`, channel settings and configuration | no | yes |
| Channel statistics, search and export | no | yes |
| Mass broadcast from General | no | yes |
| Reaction-mode configuration and service-topic management | no | yes |
| Reaction trigger in subscriber topics | yes | yes |

For every sensitive request, the bot re-reads the channel and validates the
caller’s current Telegram group-admin membership.  This is also done for
callbacks; callback data is only context, never proof of authority.  The
owner record remains intact if the owner loses Telegram admin status, but
group actions which require Telegram permissions are denied until those rights
are restored.  No role is global: the same person can be an owner of channel A
and only an ordinary administrator of channel B.
