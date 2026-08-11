# Multi-tenant Telegram Feedback Bot

Публичный бот-предложка на Python + aiogram 3.x.

## Архитектура

Один Telegram-бот обслуживает много независимых владельцев каналов.

Каждый владелец:

1. создаёт закрытую супергруппу;
2. включает в ней **Темы / Forum Topics**;
3. добавляет бота администратором;
4. выдаёт боту право **Manage Topics**;
5. желательно выдаёт **Delete Messages**;
6. отправляет в группе `/setup`.

Бот регистрирует tenant:

```text
owner_id <-> admin_group_id
```

и выдаёт ссылку:

```text
https://t.me/BOT_USERNAME?start=ref_OWNER_ID
```

Подписчик, открывший ссылку, привязывается к этому tenant.

Один подписчик может открывать ссылки нескольких владельцев.
Последняя открытая ссылка становится его активной предложкой.
Все прежние membership-связи сохраняются.

## Команды владельца в супергруппе

```text
/setup
/panel
/set_period 30
/set_announcement Ваш текст
/set_timezone Asia/Tashkent
```

`/panel` показывает кнопку ручной очистки.

## Ручная очистка

Кнопка:

```text
Очистить ветки до вчерашнего дня
```

Реализована буквальная граница из ТЗ:

```text
created_at < 00:00 вчерашнего дня
```

Например, если локальная дата tenant — 11 августа, удаляются темы,
созданные раньше 10 августа 00:00. Весь 10 и 11 августа сохраняются.

Удаление выполняется через `deleteForumTopic`.
Если Telegram не разрешает полное удаление, код пытается использовать
`closeForumTopic`.

После успешного удаления/закрытия mapping темы удаляется из SQLite.

## Авто-сброс

По умолчанию цикл — 30 дней.

За 24 часа до `next_reset_at` APScheduler начинает рассылать кастомный
текст всем подписчикам конкретного tenant.

Каждая обработанная доставка записывается в `notification_log`.
Это защищает от дублей после рестарта.

Новые подписчики, появившиеся уже внутри 24-часового окна,
также получают предупреждение на одном из следующих scheduler ticks.

В момент сброса удаляются темы с:

```text
created_at < next_reset_at
```

То есть свежая тема, созданная уже после плановой точки сброса,
не будет случайно удалена из-за небольшой задержки scheduler.

Membership пользователей сохраняется, чтобы следующие предупреждения
можно было отправлять всем подписчикам данного владельца.

## SQLite

Основные таблицы:

- `tenants`
- `users`
- `tenant_subscribers`
- `active_tenant`
- `topics`
- `notification_log`

Для SQLite используется WAL.

Важно: запускайте только **одну реплику** приложения с этой SQLite-базой.
На хостинге база должна лежать на persistent volume/disk.

## Локальный запуск

Python 3.11+ рекомендуется.

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

Заполните `BOT_TOKEN` в `.env`.

## Права бота

Минимум:

- бот является администратором forum-супергруппы;
- `can_manage_topics = true`.

Для полного удаления веток вместе с их сообщениями:

- `can_delete_messages = true`.

Без `Delete Messages` setup не блокируется, но очистка может перейти
на `closeForumTopic`.

## Деплой

Приложение использует long polling, поэтому отдельный HTTP-сервер не нужен.

Команда запуска:

```bash
python main.py
```

На Render/Railway/Fly.io/VPS выбирайте worker/background-service тип,
если платформа его поддерживает.

Критично:

- один bot token = один работающий экземпляр polling;
- SQLite-файл должен храниться на постоянном диске;
- бесплатные хостинги, которые полностью останавливают worker или удаляют
  файловую систему, не подходят для надёжного 30-дневного scheduler без
  persistent storage.


## Docker

Сборка:

```bash
docker build -t telegram-feedback-bot .
```

Запуск с постоянной SQLite-базой:

```bash
docker run --env-file .env \
  -v "$(pwd)/data:/data" \
  -e DATABASE_PATH=/data/feedback_bot.sqlite3 \
  telegram-feedback-bot
```

На Windows PowerShell путь к volume можно задать отдельно через Docker Desktop.
