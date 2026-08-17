# Subscriber preview mode

`/panel → Посмотреть глазами подписчика` — owner-only, private-chat preview основных subscriber-facing сообщений конкретного `channel_id`.

## Сценарии

Preview показывает:

- Channel Start Card, включая draft media;
- privacy prompt и безопасные неактивные preview-кнопки;
- `message.received` (production теперь отправляет ровно одно подтверждение после успешной передачи сообщения/альбома администрации);
- `message.channel_unavailable`;
- пример `sanction.applied.visible` с sample values;
- пример обычного admin reply (реальные ответы в runtime копируются из forum topic без отдельного текстового template);
- реальный `channels.notice_text`, который scheduler отправляет перед очисткой.

## Draft-aware rendering

Все сценарии, которые действительно являются Channel Custom Pack templates, рендерятся через `render_template(..., include_draft=True)`. Start Card использует `send_channel_start_card(..., include_draft=True)`. Если draft отсутствует, preview автоматически показывает опубликованную revision. Cleanup notice не подменяется template: preview берёт фактический `channels.notice_text`, потому что именно его production scheduler отправляет подписчику.

## Гарантии отсутствия production side effects

Preview intentionally не вызывает runtime mutation paths:

- не создаёт/не обновляет subscriber records;
- не меняет active subscriber channel;
- не меняет privacy mode и не выдаёт anonymous tag;
- не создаёт forum topic/topic mapping;
- не вызывает message-event analytics;
- не меняет topic status/activity;
- не применяет sanctions;
- не запускает cleanup/broadcast/reaction actions.

Privacy buttons используют `preview:subscriber:noop:*`, а не реальный `privacy:*` callback.

Preview доступен только в private chat и каждый callback повторно проходит `ChannelAction.SETTINGS`, поэтому CHANNEL_ADMIN/SUBSCRIBER и владелец чужой предложки не получают доступ.

## Системная маркировка

Маркер `ПРЕДПРОСМОТР — ничего не сохраняется` генерируется вне Channel Custom Pack. Владелец не может скрыть его кастомным template. Он показывает `channel_id` и источник: опубликованная revision либо draft поверх опубликованной revision.

## UX context

Основные owner customization screens показывают стабильный заголовок:

`Сейчас редактируется: <group> · channel_id=<id>`

и состояние оформления: активная revision + наличие/размер draft. Это снижает риск редактирования не той предложки при владении несколькими каналами.
