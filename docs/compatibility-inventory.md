# Deliberately retained compatibility paths

Этот файл отделяет необходимую совместимость от мёртвого кода. Перечисленные пути нельзя удалять как «legacy» без отдельной миграции/решения.

- `ref_<owner_id>`: старые опубликованные deep-links; разрешаются только при однозначном соответствии одному каналу.
- baseline/legacy schema validation и migration helpers в `database.py`: нужны для безопасного обновления старых production DB.
- pre-v13 moderation fallback: читает старое состояние ограничений после миграции; удалять только после отдельной data migration и срока совместимости.
- старые group-panel callback tombstones: безопасно отклоняют нажатия на сообщения, созданные до перехода на channel panel, и не дают обойти текущую авторизацию.
- `AdminGuard` fallback в sanction helpers: остаётся для изолированных compatibility/test callers; production path использует `ChannelAuthorizer` с live Telegram-admin проверкой.

Все новые функции должны использовать channel-scoped API и текущий authorization layer, а не расширять эти compatibility paths.
