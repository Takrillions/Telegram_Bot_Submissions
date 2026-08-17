# Authorization model

## Global role

`SUPERADMIN_TELEGRAM_ID` — единственный источник истины для глобального владельца экземпляра Telegram-бота. Значение задаётся только в production `.env`; оно не должно храниться в публичном репозитории. SUPERADMIN — отдельная глобальная роль и не становится автоматически владельцем любого `channel_id`.

На текущем этапе глобальное действие `PRESTART_PROFILE` (текст/медиа настоящей карточки Telegram до Start) разрешено только SUPERADMIN. Если global ID не настроен, действие закрывается fail-closed.

## Channel-scoped roles

`channels.owner_id` is the sole source of truth for the main administrator of that channel. It is assigned by the first successful `/setup` and is never changed by a callback or a repeated setup. A group administrator is not made an owner merely by their Telegram status.

| Action family | Current Telegram group administrator | Channel owner | SUPERADMIN without channel role |
| --- | --- | --- | --- |
| Work in subscriber topics, replies | yes | yes | no |
| Subscriber moderation, notes, tags, history | yes | yes | no |
| `/panel`, channel settings and configuration | no | yes | no |
| Channel statistics, search and export | no | yes | no |
| Mass broadcast from General | no | yes | no |
| Reaction-mode configuration and service-topic management | no | yes | no |
| Reaction trigger in subscriber topics | yes | yes | no |
| Global pre-Start profile | no | only if also SUPERADMIN | global permission yes; stage-1 UI entry is still through an owner panel |

For every sensitive channel request, the bot re-reads the channel and validates the caller’s current Telegram group-admin membership. This is also done for callbacks; callback data is only context, never proof of authority. The owner record remains intact if the owner loses Telegram admin status, but group actions which require Telegram permissions are denied until those rights are restored. No channel role is global: the same person can be an owner of channel A and only an ordinary administrator of channel B.

На этапе 1 глобальная авторизация уже отделена от channel authorization, но отдельный standalone SUPERADMIN-раздел панели появится на более позднем этапе. Поэтому текущий UI-вход в global pre-Start остаётся внутри `/panel` канала, которым SUPERADMIN также владеет; forged/stale callbacks всё равно повторно проверяют global role.
