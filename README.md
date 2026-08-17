# Multi-channel Telegram Feedback Bot

Публичный Telegram-бот обратной связи на Python и aiogram 3.x. Один экземпляр бота обслуживает несколько независимых каналов/супергрупп и хранит данные в SQLite.

## Основная модель

Владелец создаёт закрытую forum-супергруппу, добавляет бота администратором с `Manage Topics` и выполняет `/setup` из General. При первом подключении бот до создания `channel_id` просит задать префикс анонимных тегов (например, `Анон`); повторный `/setup` существующего канала этот префикс не меняет. Первый успешный setup фиксирует владельца конкретного `channel_id`; повторный setup не передаёт владение другому администратору.

Один владелец может подключить до пяти каналов. Подписчица может состоять сразу в нескольких каналах и выбирать активный через `/channels`. Deep-link нового формата:

```text
https://t.me/BOT_USERNAME?start=ref_c_CHANNEL_ID
```

Старый `ref_OWNER_ID` поддерживается только как совместимый вход, когда он однозначно разрешается в один канал.

## Приватность и темы

Для каждого канала подписчица выбирает отдельный режим:

- `anonymous` — администраторы видят только анонимный тег;
- `identified` — используется обычная карточка подписчицы.

Anonymous и identified используют разные forum topics. Переключение режима не отправляет новые сообщения в старую ветку. Реальный Telegram `user_id` хранится только для внутренней маршрутизации и не выводится в анонимном интерфейсе.

## Администраторы и права

`channels.owner_id` — единственный источник истины для главного администратора конкретного канала. Отдельно существует глобальный `SUPERADMIN_TELEGRAM_ID` из production `.env`: только он может менять профильные настройки самого Telegram-бота, включая настоящую карточку до Start. SUPERADMIN не получает права на чужие `channel_id` автоматически. Обычные Telegram-администраторы могут работать в пользовательских ветках и выполнять разрешённую модерацию. Настройки канала, статистика, экспорт, массовая рассылка и конфигурация реакций доступны владельцу этого канала.

Чувствительные действия повторно проверяют текущий Telegram admin status server-side; callback data не считается доказательством прав. Подробности: `docs/authorization-matrix.md`.

## Основные возможности

- multi-channel routing и channel-specific deep links;
- anonymous/identified темы;
- статусы обращений и защита тем от очистки;
- rate-limit, mute, временная/постоянная блокировка, warning, spam-mark и снятие ограничений;
- заметки, теги и история модерации;
- статистика подписчицы, канала и администраторов;
- privacy-safe поиск;
- CSV/XLSX export;
- channel-scoped редактор Telegram-реплик с обычным Telegram-форматированием и человекочитаемыми динамическими полями;
- настраиваемые anonymous prefix/counter и карточки подписчиц;
- глобальная карточка до Start, доступная только SUPERADMIN;
- отдельная channel-scoped стартовая карточка после Start: независимый текст и photo/video/GIF для каждого `channel_id`;
- массовая рассылка из General, включая albums;
- два режима реакций администраторов;
- автоматическая и ручная очистка forum topics;
- локальные и внешние SQLite backup snapshots;
- release-based deploy с readiness/rollback.

## Команды

В личном чате меню команд зависит от роли. Основные пользовательские команды:

```text
/start
/channels
/privacy
```

Владелец дополнительно получает owner-команды, включая `/panel` и `/search`.

В forum-супергруппе команды вводятся вручную. Bot API не умеет ограничивать command scope только темой General, поэтому групповое slash-меню намеренно не публикуется. Контекст каждой команды проверяется server-side. Подробности: `docs/telegram-command-scopes.md`.

В личном чате slash-меню зависит от роли: обычный пользователь видит только пользовательские команды, CHANNEL_OWNER — также `/panel` и `/search`, а настроенный SUPERADMIN — `/superadmin`. Если SUPERADMIN одновременно является владельцем предложки, команды объединяются в одном private scope.

## Настройка канала

Основная точка управления конкретной предложкой — `/panel`. Там доступны настройки канала, статистика/export, тексты, anonymous settings, cleanup и другие owner-only функции. Глобальные настройки вынесены из channel panel в отдельный private-only `/superadmin`: этот раздел доступен только пользователю, чей Telegram ID совпадает с `SUPERADMIN_TELEGRAM_ID`, и не требует владения каким-либо channel_id.

Массовая рассылка запускается `/broadcast` только владельцем из General. Подробности: `docs/mass-broadcast.md`.

Настройка реакций также выполняется владельцем из General. Подробности: `docs/reaction-routing.md`.

В private `/panel` есть owner-only режим **«Посмотреть глазами подписчика»**. Он рендерит стартовую карточку, privacy prompt, подтверждение получения сообщения, unavailable-сценарий, пример санкции, пример ответа администратора и cleanup notice. Если есть customization draft, preview накладывает его поверх опубликованной revision. Preview не регистрирует подписчика, не меняет active channel/privacy, не создаёт forum topics и не пишет message analytics. Все preview-сообщения имеют неснимаемую системную метку. Подробности: `docs/subscriber-preview.md`.

## Фундамент изолированной кастомизации

Schema v23 добавляет immutable Standard Custom Pack и per-channel Custom Pack snapshots. Для новых каналов `/setup` атомарно копирует активную Standard revision в независимый `setup_snapshot`; ошибка snapshot полностью откатывает создание канала. Повторный setup существующего канала snapshot не заменяет. Schema v24 добавляет channel-scoped media стартовой карточки, v25 расширяет безопасно кастомизируемую template surface, v26 переводит owner-редактор на persistent drafts, v27 добавляет owner-facing историю immutable revisions, audit и безопасное восстановление старой версии сначала в черновик, v28 добавляет безопасные bulk-инструменты, v29 — versioned JSON import/export Channel Custom Pack, а v30 окончательно отделяет Global Bot Profile от Standard Custom Pack и добавляет отдельный SUPERADMIN-редактор текущего стандарта. Активный Standard Pack после v30 содержит только channel-scoped templates и опциональное `start_card.media`; каждая правка создаёт новую immutable Standard revision и влияет только на будущие `/setup`. Существующие Channel Custom Pack snapshots не переписываются и не наследуют изменения автоматически. Обычный runtime читает только опубликованную immutable channel revision; черновик доступен только preview-поверхностям. `channel_template_overrides` после v26 остаётся только compatibility storage. Подробности: `docs/customization-architecture.md`, `docs/customization-bulk-tools.md` и `docs/customization-transfer-json.md`.

## SQLite и миграции

База работает в WAL-режиме и обновляется последовательными versioned migrations. Перед миграцией существующей БД создаётся проверенный локальный snapshot. Запускайте только одну polling-реплику приложения с одной SQLite-базой.

Основные сущности включают:

- channels и channel subscribers;
- active channel/privacy state;
- forum topic mappings;
- message event journal;
- sanctions/moderation metadata;
- immutable channel custom revisions + persistent customization drafts;
- broadcasts/delivery journal;
- reaction routing state;
- pre-Start card state.

## Переменные окружения

Используйте `.env.example` как шаблон. Реальный `.env` не храните в Git.

Ключевые переменные:

- `BOT_TOKEN`;
- `SUPERADMIN_TELEGRAM_ID` — числовой Telegram ID глобального владельца бота;
- `DATABASE_PATH`;
- `DATABASE_BACKUP_DIR` / `DATABASE_BACKUP_KEEP`;
- `DATABASE_REMOTE_BACKUP_BUCKET` / prefix / retention;
- `DEFAULT_TIMEZONE`;
- `DEFAULT_RESET_DAYS`;
- `DEFAULT_NOTICE_TEXT`;
- `SCHEDULER_CHECK_SECONDS`;
- `MEDIA_GROUP_DELAY`;
- `READINESS_PATH` и `RELEASE_ID` для production release layout.

## Локальный запуск

Рекомендуется Python 3.11+.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Специальные режимы:

```bash
python main.py --validate-release
python main.py --migrate-only
```

## Тесты

```bash
python -m unittest discover -v
```

Перед релизом также выполняются compile/static checks и проверка migration/deploy safety.

## Production deploy и backup

Production использует release layout с общей `.env`, БД, backups и readiness state. Деплой выполняется через `.github/workflows/deploy.yml` и `scripts/deploy_release.sh`.

Внешние backup snapshots предназначены для приватного Google Cloud Storage bucket вне основной VM. Инструкции и rollback-модель: `docs/backup-and-safe-deploy.md` и `docs/first-release-layout-transition.md`.

Нельзя вручную менять production-схему или выполнять первый переход на release layout без отдельной проверки и явного разрешения.

## Намеренно сохранённая совместимость

Некоторые legacy/fallback пути остаются специально для безопасной миграции старой БД и обработки старых Telegram callbacks/deep-links. Их список и причины сохранения находятся в `docs/compatibility-inventory.md`.


## Pre-deploy CI gate

Перед production deploy GitHub Actions запускает обязательный `verify`-job с реальными зависимостями и полным unittest suite. `deploy` имеет `needs: verify`, поэтому при ошибке проверок production не изменяется.
