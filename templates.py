"""Channel-scoped Telegram UI template registry and safe renderer."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from string import Formatter
from typing import Mapping

logger = logging.getLogger(__name__)
MAX_TEMPLATE_LENGTH = 4000


@dataclass(frozen=True)
class TemplateSpec:
    key: str
    category: str
    title: str
    default: str
    description: str
    used_in: str
    audience: str
    variables: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    allow_empty: bool = False
    scope: str = "channel"


def _spec(key, category, title, default, description, used_in, audience, variables=(), required=(), scope="channel"):
    return TemplateSpec(key, category, title, default, description, used_in, audience, frozenset(variables), frozenset(required), scope=scope)


# Defaults use feminine first-person forms whenever the bot speaks about itself.
_SPECS = (
    _spec("start.greeting", "Старт и приветствие", "Приветствие после Start", "<b>Добро пожаловать в {channel_name}.</b>\nЯ готова принять ваше обращение.", "Отправляется после deep-link канала.", "/start", "подписчица", ("channel_name",), ("channel_name",)),
    _spec("channel.selected", "Выбор канала", "Канал выбран", "Я выбрала предложку: <b>{channel_name}</b>.", "Подтверждение выбора активного канала.", "/channels", "подписчица", ("channel_name",), ("channel_name",)),
    _spec("channel.choose", "Выбор канала", "Выбор предложки", "Выберите предложку, в которую хотите написать.", "Запрос выбора при нескольких доступных каналах.", "/start", "подписчица"),
    _spec("privacy.prompt", "Приватность", "Выбор приватности", "Выберите, как отправлять обращения. Я сохраню этот выбор для канала; изменить его можно командой /privacy.", "Запрос режима приватности.", "/privacy", "подписчица"),
    _spec("message.received", "Получение сообщений", "Сообщение получено", "Я получила ваше сообщение и передала его администрации.", "Подтверждение успешной передачи.", "приём сообщения", "подписчица"),
    _spec("message.channel_unavailable", "Получение сообщений", "Предложка временно недоступна", "Сейчас предложка этого канала недоступна. Попробуйте отправить сообщение позже.", "Безопасное уведомление при потере доступа бота к административной группе.", "приём сообщения", "подписчица"),
    _spec("message.unsupported_type", "Получение сообщений", "Сообщение не передано", "Я не смогла передать этот тип сообщения. Попробуйте отправить его в другом формате.", "Безопасное уведомление при Telegram Bad Request во время передачи обращения.", "приём сообщения", "подписчица"),
    _spec("reply.user_unavailable", "Ответы", "Ответ не доставлен", "Я не смогла доставить ответ: подписчица заблокировала бота или запретила личные сообщения.", "Уведомление администратора при Telegram Forbidden во время ответа.", "ответ администратора", "администратор"),
    _spec("reply.delivery_failed", "Ответы", "Ошибка доставки ответа", "Я не смогла доставить ответ подписчице. Попробуйте ещё раз или проверьте доступность диалога.", "Безопасная ошибка Telegram при доставке ответа без раскрытия внутренних деталей.", "ответ администратора", "администратор"),
    _spec("access.denied", "Ошибки", "Нет доступа", "У вас нет доступа к этой команде.", "Единый безопасный отказ в правах.", "проверка прав", "администратор"),
    _spec("sanction.rate.applied.visible", "Ограничения и модерация", "Rate-limit с причиной", "<b>Я ввела ограничение на частоту сообщений.</b>\nТеперь можно отправлять одно сообщение раз в {duration}.\nПричина: {reason}", "Уведомление после включения постоянного rate-limit с видимой причиной.", "санкции", "подписчица", ("duration", "reason"), ("duration", "reason")),
    _spec("sanction.rate.applied.hidden", "Ограничения и модерация", "Rate-limit без причины", "<b>Я ввела ограничение на частоту сообщений.</b>\nТеперь можно отправлять одно сообщение раз в {duration}.", "Уведомление после включения постоянного rate-limit без причины.", "санкции", "подписчица", ("duration",), ("duration",)),
    _spec("sanction.rate.active.visible", "Ограничения и модерация", "Активный rate-limit с причиной", "<b>Отправка временно ограничена.</b>\nПовторите после: {expires_at}.\nПричина: {reason}", "Повторное уведомление при rate-limit.", "санкции", "подписчица", ("expires_at", "reason"), ("expires_at", "reason")),
    _spec("sanction.rate.active.hidden", "Ограничения и модерация", "Активный rate-limit без причины", "<b>Отправка временно ограничена.</b>\nПовторите после: {expires_at}.", "Повторное уведомление без причины.", "санкции", "подписчица", ("expires_at",), ("expires_at",)),
    _spec("sanction.applied.visible", "Ограничения и модерация", "Санкция с причиной", "<b>{action}</b> применено. {duration}\nПричина: {reason}", "Уведомление о mute/block/warning.", "санкции", "подписчица", ("action", "duration", "reason"), ("action", "duration")),
    _spec("sanction.applied.hidden", "Ограничения и модерация", "Санкция без причины", "<b>{action}</b> применено. {duration}", "Уведомление без причины.", "санкции", "подписчица", ("action", "duration"), ("action", "duration")),
    _spec("sanction.active.visible", "Ограничения и модерация", "Активная санкция с причиной", "<b>{action}</b> действует. {duration}\nПричина: {reason}", "Повторное уведомление об active mute/block.", "санкции", "подписчица", ("action", "duration", "reason"), ("action", "duration")),
    _spec("sanction.active.hidden", "Ограничения и модерация", "Активная санкция без причины", "<b>{action}</b> действует. {duration}", "Повторное уведомление об active sanction.", "санкции", "подписчица", ("action", "duration"), ("action", "duration")),
    _spec("cleanup.notice", "Автоочистка", "Предупреждение об очистке", "Я подготовила предупреждение: история предложки будет очищена через 24 часа.", "Рассылка scheduler до очистки.", "scheduler", "подписчица"),
    _spec("search.empty", "Поиск", "Поиск без результатов", "Я не нашла ничего по этому запросу.", "Пустой результат поиска.", "поиск", "администратор"),
    _spec("statistics.empty", "Статистика", "Нет статистики", "Пока нет данных за выбранный период.", "Пустая статистика.", "статистика", "администратор"),
    _spec("export.failed", "Экспорт", "Ошибка экспорта", "Я не смогла подготовить файл экспорта. Попробуйте ещё раз.", "Ошибка доставки export.", "экспорт", "администратор"),
    _spec("panel.welcome", "Панель", "Панель управления", "<b>Панель предложки</b>\nЯ подготовила актуальные настройки канала {channel_name}.", "Заголовок административной панели.", "/panel", "главный администратор", ("channel_name",), ("channel_name",)),

    # Sanction FSM and subscriber metadata. Keeping these as separate keys makes
    # channel-level styling available without exposing flow internals.
    _spec("sanction.flow.invalid_callback", "Ограничения и модерация", "Недоступное действие", "Это действие больше недоступно.", "Безопасный ответ для устаревшего или некорректного moderation callback.", "sanction FSM", "администратор"),
    _spec("sanction.flow.access_denied", "Ограничения и модерация", "Доступ запрещён", "Нет прав или актуального контекста для этого действия.", "Отказ при потере прав или контекста moderation flow.", "sanction FSM", "администратор"),
    _spec("sanction.flow.choose_duration", "Ограничения и модерация", "Выбор срока", "Выберите срок. Санкция будет применена только после выбора причины и подтверждения.", "Запрос срока временного ограничения.", "sanction FSM", "администратор"),
    _spec("sanction.flow.custom_duration", "Ограничения и модерация", "Свой срок", "Введите срок в минутах: от 1 до 10080.", "Ввод собственного срока ограничения.", "sanction FSM", "администратор"),
    _spec("sanction.flow.invalid_duration", "Ограничения и модерация", "Некорректный срок", "Укажите целое число минут от 1 до 10080.", "Ошибка проверки срока.", "sanction FSM", "администратор"),
    _spec("sanction.flow.cancelled", "Ограничения и модерация", "Санкция отменена", "Применение санкции отменено.", "Отмена sanction FSM.", "sanction FSM", "администратор"),
    _spec("sanction.flow.expired", "Ограничения и модерация", "Сценарий устарел", "Сценарий санкции устарел. Начните заново из карточки подписчицы.", "Устаревшее состояние или callback.", "sanction FSM", "администратор"),
    _spec("sanction.flow.choose_reason", "Ограничения и модерация", "Выбор причины", "Выберите причину санкции.", "Запрос причины санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.custom_reason", "Ограничения и модерация", "Своя причина", "Введите свою причину санкции.", "Ввод собственной причины санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.invalid_reason", "Ограничения и модерация", "Некорректная причина", "Причина не может быть пустой. Введите текст ещё раз.", "Ошибка проверки причины.", "sanction FSM", "администратор"),
    _spec("sanction.flow.choose_visibility", "Ограничения и модерация", "Показывать причину", "Показывать причину подписчице?", "Выбор видимости причины санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.callback_expected", "Ограничения и модерация", "Ожидается кнопка", "Используйте кнопки текущего шага или отмените сценарий.", "Ответ на текст вместо ожидаемого callback.", "sanction FSM", "администратор"),
    _spec("sanction.flow.apply_failed", "Ограничения и модерация", "Санкция не применена", "Санкцию не удалось применить. Откройте карточку подписчицы и попробуйте ещё раз.", "Безопасная ошибка применения санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.delivery_sent", "Ограничения и модерация", "Уведомление отправлено", "Уведомление подписчице отправлено.", "Статус доставки после санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.delivery_failed", "Ограничения и модерация", "Уведомление не доставлено", "Санкция применена, но уведомление подписчице доставить не удалось.", "Ошибка уведомления после санкции.", "sanction FSM", "администратор"),
    _spec("sanction.flow.no_active", "Ограничения и модерация", "Нет ограничений", "Активных ограничений нет.", "Пустой список активных ограничений.", "/subscriber", "администратор"),
    _spec("sanction.flow.clear_confirmation", "Ограничения и модерация", "Подтверждение снятия", "Снять ограничения: {actions}?", "Подтверждение снятия выбранных санкций.", "/subscriber", "администратор", ("actions",), ("actions",)),
    _spec("sanction.flow.cleared", "Ограничения и модерация", "Ограничения сняты", "Снято ограничений: {count}.", "Результат снятия санкций.", "/subscriber", "администратор", ("count",), ("count",)),
    _spec("subscriber.metadata.note_prompt", "Подписчица", "Новая заметка", "Введите внутреннюю заметку для администрации.", "Добавление заметки.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.tag_prompt", "Подписчица", "Новый тег", "Введите тег для подписчицы.", "Добавление тега.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.invalid_text", "Подписчица", "Некорректный текст", "Текст не может быть пустым или слишком длинным. Введите его ещё раз.", "Проверка заметки или тега.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.saved_note", "Подписчица", "Заметка сохранена", "Внутренняя заметка сохранена.", "Результат добавления заметки.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.saved_tag", "Подписчица", "Тег сохранён", "Тег сохранён.", "Результат добавления тега.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.duplicate_tag", "Подписчица", "Тег уже существует", "Такой тег уже есть у подписчицы.", "Case-insensitive duplicate tag.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.cancelled", "Подписчица", "Действие отменено", "Действие отменено.", "Отмена metadata FSM.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.expired", "Подписчица", "Действие устарело", "Это действие больше недоступно.", "Устаревшее состояние metadata flow.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.access_denied", "Подписчица", "Нет доступа", "Нет прав или актуального контекста для этого действия.", "Отказ metadata flow.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.notes_title", "Подписчица", "Список заметок", "<b>Заметки: {target}</b>\nСтраница {page}/{pages}", "Заголовок списка заметок.", "/subscriber", "администратор", ("target", "page", "pages"), ("target", "page", "pages")),
    _spec("subscriber.metadata.tags_title", "Подписчица", "Список тегов", "<b>Теги: {target}</b>\nСтраница {page}/{pages}", "Заголовок списка тегов.", "/subscriber", "администратор", ("target", "page", "pages"), ("target", "page", "pages")),
    _spec("subscriber.metadata.empty_notes", "Подписчица", "Нет заметок", "Заметок пока нет.", "Пустой список заметок.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.empty_tags", "Подписчица", "Нет тегов", "Тегов пока нет.", "Пустой список тегов.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.note_title", "Подписчица", "Просмотр заметки", "<b>Заметка: {target}</b>\nСоздана: {created}\n\n{text}", "Просмотр одной заметки.", "/subscriber", "администратор", ("target", "created", "text"), ("target", "created", "text")),
    _spec("subscriber.metadata.edit_prompt", "Подписчица", "Редактирование заметки", "Введите новый текст заметки.", "Ввод нового текста заметки.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.edit_confirmation", "Подписчица", "Подтверждение заметки", "Сохранить новый текст заметки?\n\n{text}", "Подтверждение редактирования заметки.", "/subscriber", "администратор", ("text",), ("text",)),
    _spec("subscriber.metadata.delete_note_confirmation", "Подписчица", "Удаление заметки", "Удалить заметку?\n\n{text}", "Подтверждение удаления заметки.", "/subscriber", "администратор", ("text",), ("text",)),
    _spec("subscriber.metadata.delete_tag_confirmation", "Подписчица", "Удаление тега", "Удалить тег «{tag}»?", "Подтверждение удаления тега.", "/subscriber", "администратор", ("tag",), ("tag",)),
    _spec("subscriber.metadata.note_updated", "Подписчица", "Заметка изменена", "Заметка изменена.", "Результат редактирования заметки.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.note_deleted", "Подписчица", "Заметка удалена", "Заметка удалена.", "Результат удаления заметки.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.tag_deleted", "Подписчица", "Тег удалён", "Тег удалён.", "Результат удаления тега.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.not_found", "Подписчица", "Запись не найдена", "Запись больше не найдена.", "Удалённая заметка или тег.", "/subscriber", "администратор"),
)


_SEARCH_SPECS = (
    _spec("search.prompt", "Поиск", "Запрос поиска", "Введите имя, username или anonymous tag для поиска. Отмена — /panel.", "Начало поиска.", "поиск", "администратор"),
    _spec("search.invalid_query", "Поиск", "Некорректный запрос", "Введите непустой запрос длиной до 96 символов.", "Валидация поиска.", "поиск", "администратор"),
    _spec("search.unavailable", "Поиск", "Поиск недоступен", "Поиск сейчас недоступен. Откройте панель и выберите доступный канал.", "Нет channel-контекста.", "поиск", "администратор"),
    _spec("search.stale", "Поиск", "Устаревший поиск", "Поиск устарел. Начните его заново.", "Stale FSM/callback.", "поиск", "администратор"),
    _spec("search.invalid_callback", "Поиск", "Некорректное действие", "Это действие поиска больше недоступно.", "Повреждённый callback.", "поиск", "администратор"),
    _spec("search.results", "Поиск", "Результаты поиска", "<b>Результаты поиска</b> — {count}", "Заголовок списка результатов.", "поиск", "администратор", ("count",), ("count",)),
    _spec("search.result_unavailable", "Поиск", "Результат недоступен", "Выбранный результат больше недоступен.", "Удалённая связь подписчицы.", "поиск", "администратор"),
    _spec("search.topic_unavailable", "Поиск", "Ветка недоступна", "<b>{display_name}</b>\nВетка больше недоступна.", "Исторический результат без forum topic.", "поиск", "администратор", ("display_name",), ("display_name",)),
    _spec("search.open_result", "Поиск", "Открытие результата", "<b>{display_name}</b>\nОткройте ветку и используйте /subscriber для полной карточки.", "Переход к существующей карточке.", "поиск", "администратор", ("display_name",), ("display_name",)),
)
_SPECS = _SPECS + _SEARCH_SPECS


_STAT_EXPORT_SPECS=(
 _spec("statistics.page.overview","Статистика","Страница обзора","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Обзор канальной статистики.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.page.messages","Статистика","Страница сообщений","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Разбивка сообщений и альбомов.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.page.responses","Статистика","Страница ответов","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Метрики первых ответов.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.page.activity","Статистика","Страница активности","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Часы и дни активности.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.page.top","Статистика","Топ подписчиц","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Безопасный Top-5.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.admins","Статистика","Статистика администраторов","<b>Статистика — {page_title}</b>\nПериод: <b>{period}</b>\n\n{body}{legacy_warning}","Сводка работы администрации.","/stats","администратор",("page_title","period","body","legacy_warning"),("page_title","period","body")),
 _spec("statistics.legacy_warning","Статистика","Неполные исторические данные","Для части старых обращений детальная статистика ответов недоступна.","Показывается при отсутствии conversation tracking.","/stats","администратор"),
 _spec("statistics.unavailable","Статистика","Статистика недоступна","Статистика сейчас недоступна. Откройте актуальную панель канала.","Ошибка доступа, stale callback или disabled channel.","/stats","администратор"),
 _spec("statistics.no_data","Статистика","Нет данных","Нет данных за выбранный период.","Пустая страница статистики.","/stats","администратор"),
 _spec("export.choose_format","Экспорт","Выбор формата","Выберите формат экспорта.","Export format selection.","/panel → Статистика","главный администратор"),
 _spec("export.preparing","Экспорт","Подготовка файла","Я подготавливаю файл экспорта.","Export preparation status.","/panel → Статистика","главный администратор"),
 _spec("export.sent","Экспорт","Файл отправлен","Я подготовила и отправила файл экспорта.","Successful export delivery.","/panel → Статистика","главный администратор"),
 _spec("export.too_large","Экспорт","Слишком большой файл","Файл экспорта слишком большой для отправки.","Telegram export size limit.","/panel → Статистика","главный администратор"),
 _spec("export.delivery_failed","Экспорт","Ошибка доставки файла","Я не смогла отправить файл экспорта. Попробуйте ещё раз.","Safe export delivery failure.","/panel → Статистика","главный администратор"),
 _spec("export.unavailable","Экспорт","Экспорт недоступен","Экспорт сейчас недоступен. Откройте актуальную панель канала.","Stale export or unavailable channel.","/panel → Статистика","главный администратор"),
)
_SPECS=_SPECS+_STAT_EXPORT_SPECS

_PANEL_SETTINGS_SPECS = (
    _spec("panel.overview", "Панель", "Обзор панели", "<b>Панель предложки</b>\n\nГруппа: {channel_name}\nПодписчиков: <b>{subscribers}</b>\nАктивных тем: <b>{topics}</b>\n\nПериод автоочистки: <b>{period_days} дней</b>\nСледующий сброс: <b>{next_reset}</b>\n\n<b>Диплинк:</b>\n<code>{deep_link}</code>\n\n<b>Предупреждение за 24 часа:</b>\n{notice_text}\n\n<code>/set_period 30</code> — период\n<code>/set_announcement текст</code> — анонс\n<code>/set_timezone Europe/Moscow</code> — часовой пояс", "Главный экран /panel.", "/panel", "главный администратор", ("channel_name","subscribers","topics","period_days","timezone","next_reset","deep_link","notice_text"), ("channel_name","subscribers","topics","period_days","timezone","next_reset","deep_link","notice_text")),
    _spec("panel.no_channels", "Панель", "Нет доступных каналов", "У вас пока нет доступных предложек. Подключите канал через /setup.", "Private /panel без каналов.", "/panel", "главный администратор"),
    _spec("panel.choose_channel", "Панель", "Выбор канала", "Выберите предложку для управления.", "Private /panel при нескольких каналах.", "/panel", "главный администратор"),
    _spec("panel.unavailable", "Панель", "Панель недоступна", "Панель сейчас недоступна. Откройте её заново из доступного канала.", "Stale callback, lost rights or disabled channel.", "/panel", "главный администратор"),
    _spec("panel.channel_selected", "Панель", "Канал выбран", "Я открыла панель выбранной предложки.", "Подтверждение выбора active channel.", "/panel", "главный администратор"),
    _spec("panel.notices", "Панель", "Текст предупреждения", "<b>Предупреждение</b>\n\nЗа 24 часа до автоматической очистки подписчицам отправляется:\n{notice_text}\n\nИзменить текст: <code>/set_announcement текст</code>", "Экран текста notice.", "/panel", "главный администратор", ("notice_text",), ("notice_text",)),
    _spec("panel.texts", "Панель", "Редактор текстов", "<b>Тексты и оформление</b>\nВыберите категорию шаблонов.", "Вход в template editor.", "/panel", "главный администратор"),
    _spec("settings.period_usage", "Настройки канала", "Неверный период", "Использование: <code>/set_period 30</code>\nДопустимо от 2 до 3650 дней.", "Валидация /set_period.", "/set_period", "главный администратор"),
    _spec("settings.period_saved", "Настройки канала", "Период сохранён", "Период установлен: <b>{days} дней</b>.\nНовый отсчёт начат сейчас.\nСледующий сброс: <b>{next_reset}</b>.", "Успешный /set_period.", "/set_period", "главный администратор", ("days","next_reset"), ("days","next_reset")),
    _spec("settings.notice_usage", "Настройки канала", "Нужен текст предупреждения", "Использование:\n<code>/set_announcement Текст предупреждения</code>", "Пустой /set_announcement.", "/set_announcement", "главный администратор"),
    _spec("settings.notice_too_long", "Настройки канала", "Слишком длинный текст", "Текст слишком длинный. Максимум 4000 символов.", "Валидация notice.", "/set_announcement", "главный администратор"),
    _spec("settings.notice_saved", "Настройки канала", "Предупреждение сохранено", "Текст предупреждения сохранён.", "Успешный /set_announcement.", "/set_announcement", "главный администратор"),
    _spec("settings.topic_template_usage", "Настройки канала", "Формат имени темы", "Использование: <code>/set_topic_template identified {{name}} · {{username}}</code>\nДля anonymous: <code>/set_topic_template anonymous {{anonymous_tag}}</code>", "Валидация topic template.", "/set_topic_template", "главный администратор"),
    _spec("settings.topic_template_invalid", "Настройки канала", "Неверный формат темы", "Шаблон не принят: {error}. Разрешённые переменные: {allowed}.", "Ошибка topic template.", "/set_topic_template", "главный администратор", ("error","allowed"), ("error","allowed")),
    _spec("settings.topic_template_saved", "Настройки канала", "Формат темы сохранён", "Шаблон имени темы сохранён.", "Успешный topic template.", "/set_topic_template", "главный администратор"),
    _spec("settings.timezone_usage", "Настройки канала", "Нужен часовой пояс", "Использование: <code>/set_timezone Asia/Tashkent</code>", "Пустой /set_timezone.", "/set_timezone", "главный администратор"),
    _spec("settings.timezone_invalid", "Настройки канала", "Неизвестный часовой пояс", "Неизвестный часовой пояс. Используйте IANA-имя, например <code>Asia/Tashkent</code> или <code>Europe/Moscow</code>.", "Ошибка timezone.", "/set_timezone", "главный администратор"),
    _spec("settings.timezone_saved", "Настройки канала", "Часовой пояс сохранён", "Часовой пояс сохранён: <code>{timezone}</code>", "Успешный /set_timezone.", "/set_timezone", "главный администратор", ("timezone",), ("timezone",)),
    _spec("settings.anonymous_overview", "Настройки канала", "Анонимные теги", "<b>Анонимные теги</b>\n\nТекущий префикс: <b>{prefix}</b>\nСледующий свободный номер: <b>{next_number}</b>\n\nИзменение префикса не меняет уже выданные теги и названия существующих веток.", "Экран настройки анонимного префикса и счётчика.", "/panel → Анонимность", "главный администратор", ("prefix","next_number"), ("prefix","next_number")),
    _spec("settings.anonymous_edit_prompt", "Настройки канала", "Новый анонимный префикс", "Отправьте новый префикс анонимного тега одним сообщением. Допустимо от 1 до 32 символов. Например: <code>Анон</code>.", "Запрос нового префикса.", "/panel → Анонимность", "главный администратор"),
    _spec("settings.anonymous_invalid", "Настройки канала", "Некорректный анонимный префикс", "Префикс не сохранён. Укажите от 1 до 32 видимых символов.", "Ошибка проверки анонимного префикса.", "/panel → Анонимность", "главный администратор"),
    _spec("settings.anonymous_saved", "Настройки канала", "Анонимный префикс сохранён", "Префикс сохранён: <b>{prefix}</b>. Уже выданные теги и существующие ветки не переименованы.", "Успешное изменение анонимного префикса.", "/panel → Анонимность", "главный администратор", ("prefix",), ("prefix",)),
    _spec("settings.anonymous_private_required", "Настройки канала", "Откройте личную панель", "Изменение анонимного префикса выполняется в личном чате с ботом через /panel.", "Попытка начать текстовый settings-flow из супергруппы.", "/panel → Анонимность", "главный администратор"),
    _spec("cleanup.overview", "Настройки канала", "Автоочистка", "<b>Автоочистка</b>\n\nСостояние: <b>{enabled}</b>\nПериод: <b>{period_days} дней</b>\nОснова: {basis}\nОхват: {scope}\nДействие: {action}\nОкончательное удаление через: {final_delete_days} дней\n\nВыберите действие, затем при необходимости скорректируйте параметры.", "Экран политики cleanup.", "/panel", "главный администратор", ("enabled","period_days","basis","scope","action","final_delete_days"), ("enabled","period_days","basis","scope","action","final_delete_days")),
    _spec("cleanup.enable_prompt", "Настройки канала", "Включение автоочистки", "<b>Включить автоочистку</b>\n\nВыберите новый период. Политика очистки сохранится.", "Выбор периода включения.", "/panel", "главный администратор"),
    _spec("cleanup.manual_preview", "Настройки канала", "Предпросмотр очистки", "Кандидатов на очистку: <b>{count}</b>. Проверьте параметры перед подтверждением.", "Preview ручной очистки.", "/panel", "главный администратор", ("count",), ("count",)),
    _spec("cleanup.manual_complete", "Настройки канала", "Очистка завершена", "Очистка завершена. Удалено: {deleted}; закрыто: {closed}; ошибок: {failed}.", "Результат ручной очистки.", "/panel", "главный администратор", ("deleted","closed","failed"), ("deleted","closed","failed")),
    _spec("cleanup.manual_complete_reset", "Настройки канала", "Очистка и нумерация сброшены", "Очистка завершена. Удалено: {deleted}; закрыто: {closed}; ошибок: {failed}. Анонимная нумерация нового цикла начнётся с 1.", "Результат ручной очистки со сбросом anonymous counter.", "/panel", "главный администратор", ("deleted","closed","failed"), ("deleted","closed","failed")),
    _spec("cleanup.manual_reset_skipped", "Настройки канала", "Нумерация не сброшена", "Очистка завершилась с ошибками, поэтому анонимная нумерация не была сброшена. Исправьте ошибки и повторите полный сброс.", "Защита от смены anonymous cycle при частично неуспешной очистке.", "/panel", "главный администратор"),
)
_SPECS = _SPECS + _PANEL_SETTINGS_SPECS

_SETUP_SPECS = (
    _spec("setup.supergroup_required", "Подключение канала", "Нужна супергруппа", "Команду /setup нужно отправить в закрытой супергруппе с включёнными Темами.", "Проверка контекста /setup.", "/setup", "администратор", scope="global"),
    _spec("setup.topic_context_invalid", "Подключение канала", "Неверный контекст", "Запустите /setup в General, а не в пользовательской ветке.", "Защита setup из forum topic.", "/setup", "администратор", scope="global"),
    _spec("setup.forum_required", "Подключение канала", "Нужны темы", "В этой супергруппе не включены Темы. Сначала включите их в настройках группы.", "Проверка Forum Topics.", "/setup", "администратор", scope="global"),
    _spec("setup.anonymous_caller", "Подключение канала", "Нужен аккаунт", "/setup нельзя выполнить анонимно. Отправьте команду от своего аккаунта.", "Проверка инициатора.", "/setup", "администратор", scope="global"),
    _spec("setup.caller_not_admin", "Подключение канала", "Недостаточно прав", "/setup может выполнить только администратор этой группы.", "Проверка Telegram admin инициатора.", "/setup", "администратор", scope="global"),
    _spec("setup.bot_not_admin", "Подключение канала", "Бот не администратор", "Сначала сделайте бота администратором этой супергруппы.", "Проверка прав бота.", "/setup", "администратор", scope="global"),
    _spec("setup.bot_missing_topics", "Подключение канала", "Не хватает права", "Боту не хватает права <b>Управление темами / Manage Topics</b>. Выдайте его и повторите /setup.", "Проверка Manage Topics.", "/setup", "администратор", scope="global"),
    _spec("setup.channel_limit", "Подключение канала", "Лимит каналов", "Достигнут лимит: один владелец может подключить не более 5 каналов.", "Лимит 5 channel.", "/setup", "администратор", scope="global"),
    _spec("setup.owner_conflict", "Подключение канала", "Группа уже подключена", "Эта супергруппа уже подключена другим главным администратором.", "Защита от перехвата channel.", "/setup", "администратор", scope="global"),
    _spec("setup.failed", "Подключение канала", "Setup не завершён", "Я не смогла завершить подключение. Проверьте права бота и повторите /setup.", "Безопасная ошибка Telegram/registration.", "/setup", "администратор", scope="global"),
    _spec("setup.anonymous_prefix_prompt", "Подключение канала", "Префикс анонимных тегов", "Перед подключением задайте префикс анонимных подписчиц. Отправьте его одним сообщением: от 1 до 32 символов. Например: <code>Анон</code>. Тогда теги будут выглядеть как <code>Анон-1</code>, <code>Анон-2</code> и далее.", "Обязательный выбор anonymous prefix при первом /setup до создания channel.", "/setup", "главный администратор", scope="global"),
    _spec("setup.anonymous_prefix_invalid", "Подключение канала", "Некорректный префикс", "Префикс не принят. Отправьте от 1 до 32 видимых символов одним сообщением.", "Ошибка anonymous prefix во время первого /setup; состояние setup сохраняется для повторного ввода.", "/setup", "главный администратор", scope="global"),
    _spec("setup.success.created", "Подключение канала", "Канал подключён", "<b>Предложка {channel_name} подключена.</b>\n\nПерсональная ссылка для подписчиц:\n<code>{deep_link}</code>\n\nОпубликуйте её в своём канале. Открывшие ссылку подписчицы будут направлены именно в эту супергруппу.{warning}", "Успешное первое или дополнительное подключение.", "/setup", "главный администратор", ("channel_name","deep_link","warning"), ("channel_name","deep_link")),
    _spec("setup.success.existing", "Подключение канала", "Setup повторён", "<b>Предложка {channel_name} уже была подключена.</b>\n\nАктуальная персональная ссылка:\n<code>{deep_link}</code>\n\nПовторный /setup не создаёт новую предложку.{warning}", "Повторный setup тем же owner.", "/setup", "главный администратор", ("channel_name","deep_link","warning"), ("channel_name","deep_link")),
    _spec("setup.warning_delete_permission", "Подключение канала", "Нет Delete Messages", "\n\n<b>Предупреждение:</b> у бота нет права <b>Удаление сообщений / Delete Messages</b>. Создание тем продолжит работать, но при очистке бот сможет только попытаться закрыть тему.", "Дополнение к setup success.", "/setup", "главный администратор"),
    _spec("setup.deep_link_invalid", "Подключение канала", "Некорректная ссылка", "Ссылка приглашения некорректна.", "Некорректный /start ref payload.", "/start", "подписчица", scope="global"),
    _spec("setup.deep_link_unavailable", "Подключение канала", "Ссылка недоступна", "Эта ссылка больше не ведёт к доступной предложке.", "Удалённый или disabled ref channel.", "/start", "подписчица", scope="global"),
)
_CHANNEL_PRIVACY_SPECS = (
    _spec("channel.no_available", "Выбор канала", "Нет доступных предложек", "У вас пока нет доступных предложек. Откройте персональную ссылку нужного канала.", "Пустой список доступных каналов.", "/channels", "подписчица", scope="global"),
    _spec("channel.choose_current", "Выбор канала", "Выбор другой предложки", "Текущая предложка: <b>{channel_name}</b>. Выберите другую предложку.", "Выбор channel при нескольких доступных.", "/channels", "подписчица", ("channel_name",), ("channel_name",)),
    _spec("channel.unavailable", "Выбор канала", "Канал недоступен", "Эта предложка больше недоступна. Выберите другую через /channels.", "Stale, invalid or disabled channel selection.", "/channels", "подписчица", scope="global"),
    _spec("privacy.no_active_channel", "Приватность", "Не выбрана предложка", "Сначала выберите доступную предложку командой /channels.", "Вызов /privacy без trusted active channel.", "/privacy", "подписчица", scope="global"),
    _spec("privacy.current_anonymous", "Приватность", "Текущий анонимный режим", "Сейчас обращения отправляются анонимно. Ваш тег: <b>{anonymous_tag}</b>.", "Текущий anonymous privacy mode.", "/privacy", "подписчица", ("anonymous_tag",), ("anonymous_tag",)),
    _spec("privacy.current_identified", "Приватность", "Текущий открытый режим", "Сейчас обращения отправляются в открытом режиме.", "Текущий identified privacy mode.", "/privacy", "подписчица"),
    _spec("privacy.switched_anonymous", "Приватность", "Включён анонимный режим", "Анонимный режим включён. Ваш тег: <b>{anonymous_tag}</b>. Дальнейшие обращения будут отправляться анонимно.", "Успешное переключение в anonymous.", "/privacy", "подписчица", ("anonymous_tag",), ("anonymous_tag",)),
    _spec("privacy.switched_identified", "Приватность", "Включён открытый режим", "Открытый режим включён. Дальнейшие обращения будут отправляться от вашего профиля.", "Успешное переключение в identified.", "/privacy", "подписчица"),
    _spec("privacy.already_anonymous", "Приватность", "Анонимный режим уже включён", "Анонимный режим уже включён. Ваш тег: <b>{anonymous_tag}</b>.", "Повторный выбор anonymous.", "/privacy", "подписчица", ("anonymous_tag",), ("anonymous_tag",)),
    _spec("privacy.already_identified", "Приватность", "Открытый режим уже включён", "Открытый режим уже включён.", "Повторный выбор identified.", "/privacy", "подписчица"),
    _spec("privacy.unavailable", "Приватность", "Настройка приватности недоступна", "Настройка приватности сейчас недоступна. Откройте /channels и выберите доступную предложку.", "Stale callback, invalid mode or unavailable channel.", "/privacy", "подписчица", scope="global"),
)
_SUBSCRIBER_CARD_SPECS = (
    _spec("subscriber.card.identified", "Карточка подписчицы", "Открытая карточка", "<b>Карточка подписчицы</b>\nИмя: {name}\nUsername: {username}\nTelegram ID: <code>{user_id}</code>\nПервое сообщение: {first_seen}\nКоличество обращений: {message_count}\nПоследняя активность: {last_seen}", "Первое служебное сообщение новой identified-ветки.", "создание пользовательской темы", "администратор", ("name","username","user_id","first_seen","message_count","last_seen"), ("name","username","user_id","first_seen","message_count","last_seen")),
    _spec("subscriber.card.anonymous", "Карточка подписчицы", "Анонимная карточка", "<b>Анонимное обращение</b>\nТег: <b>{anonymous_tag}</b>\nАнонимных сообщений в этом канале: {message_count}\nПоследняя активность: {last_seen}", "Первое служебное сообщение новой anonymous-ветки без раскрытия личности.", "создание пользовательской темы", "администратор", ("anonymous_tag","message_count","last_seen"), ("anonymous_tag","message_count","last_seen")),
)
_SPECS = _SPECS + _SUBSCRIBER_CARD_SPECS

_STATUS_TOPIC_SPECS = (
    _spec("status.context_required", "Статусы", "Нужна пользовательская тема", "Используйте /status только внутри пользовательской темы.", "Команда /status вне пользовательской forum-темы.", "/status", "администратор", scope="global"),
    _spec("status.unavailable", "Статусы", "Тема недоступна", "Эта пользовательская тема больше недоступна.", "Устаревшая или неизвестная пользовательская тема.", "/status", "администратор", scope="global"),
    _spec("status.overview", "Статусы", "Состояние обращения", "<b>Состояние обращения</b>\nСтатус: <b>{status}</b>\nВажная: <b>{important}</b>\nЗащита от очистки: <b>{pinned}</b>\n\nВыберите новое состояние или измените защиту от автоочистки.", "Карточка управления статусом и защитой ветки.", "/status", "администратор", ("status", "important", "pinned"), ("status", "important", "pinned")),
    _spec("status.usage", "Статусы", "Неизвестный статус", "Неизвестное состояние. Используйте кнопки ниже или: <code>/status new | in_progress | answered | closed</code>.", "Ошибка ручного аргумента /status.", "/status", "администратор"),
    _spec("status.changed", "Статусы", "Статус изменён", "Статус изменён: <b>{status}</b>.", "Подтверждение ручной смены статуса.", "/status", "администратор", ("status",), ("status",)),
    _spec("status.protection_changed", "Статусы", "Защита изменена", "Защита ветки от автоочистки обновлена.", "Подтверждение изменения important/pinned protection.", "/status", "администратор"),
)

_BROADCAST_SPECS = (
    _spec("broadcast.general_required", "Рассылка", "Рассылка только из General", "Запустить массовую рассылку можно только командой /broadcast в теме General подключённой супергруппы.", "Безопасный отказ вне General.", "/broadcast", "главный администратор", scope="global"),
    _spec("broadcast.owner_required", "Рассылка", "Рассылка только для главного администратора", "Массовую рассылку может запускать только главный администратор этой предложки.", "Отказ обычному администратору или при потере прав.", "/broadcast", "администратор", scope="global"),
    _spec("broadcast.prompt", "Рассылка", "Введите сообщение рассылки", "Отправьте одно сообщение или один медиаальбом, который нужно опубликовать во всех актуальных пользовательских ветках этой предложки. Я сначала покажу предпросмотр.", "Начало сценария рассылки.", "/broadcast", "главный администратор"),
    _spec("broadcast.unsupported", "Рассылка", "Тип сообщения не поддерживается", "Это сообщение нельзя безопасно скопировать как рассылку. Отправьте текст, фото, видео, документ, аудио, голосовое сообщение, GIF, стикер, опрос или другой обычный копируемый тип.", "Отказ для service/unsupported message.", "/broadcast", "главный администратор"),
    _spec("broadcast.preview_ready", "Рассылка", "Предпросмотр готов", "Предпросмотр готов. Проверьте публикацию и выберите действие: отправить, заменить сообщение или отменить рассылку.", "Подпись к предпросмотру рассылки.", "/broadcast", "главный администратор"),
    _spec("broadcast.cancelled", "Рассылка", "Рассылка отменена", "Рассылка отменена. Ничего не отправлено.", "Отмена draft рассылки.", "/broadcast", "главный администратор"),
    _spec("broadcast.conflict", "Рассылка", "Рассылка уже выполняется", "Для этой предложки уже выполняется рассылка. Дождитесь её завершения или восстановите незавершённую отправку.", "Запрет конфликтующих рассылок одного channel.", "/broadcast", "главный администратор"),
    _spec("broadcast.resume_available", "Рассылка", "Есть незавершённая рассылка", "Я нашла незавершённую рассылку этой предложки. Можно безопасно продолжить её: уже зарезервированные получатели повторно не отправляются.", "Восстановление после перезапуска процесса.", "/broadcast", "главный администратор"),
    _spec("broadcast.started", "Рассылка", "Рассылка запущена", "Рассылка запущена. Я отправляю публикацию только в актуальные пользовательские ветки.", "Подтверждение запуска после явной кнопки Отправить.", "/broadcast", "главный администратор"),
    _spec("broadcast.summary", "Рассылка", "Итог рассылки", "<b>Рассылка завершена.</b>\nУникальных получателей: {unique_recipients}\nУспешно доставлено: {delivered}\nНедоставлено: {undelivered}\nПропущено: {skipped}\nОшибок: {errors}", "Итоговая статистика одной рассылки.", "/broadcast", "главный администратор", ("unique_recipients","delivered","undelivered","skipped","errors"), ("unique_recipients","delivered","undelivered","skipped","errors")),
    _spec("broadcast.unavailable", "Рассылка", "Рассылка недоступна", "Эта рассылка больше недоступна или ваши права изменились.", "Stale/forged callback и потеря доступа.", "/broadcast", "главный администратор", scope="global"),
)

_REACTION_SPECS = (
    _spec("reaction.general_required", "Реакции", "Настройка только из General", "Настройки реакций доступны только главному администратору из темы General подключённой супергруппы.", "Безопасный отказ вне General.", "/panel → Реакции", "главный администратор", scope="global"),
    _spec("reaction.settings_overview", "Реакции", "Настройки реакций", "<b>Реакции администраторов</b>\nРежим: <b>{mode}</b>\nСлужебная ветка: <b>{topic}</b>\nСостояние: {repair}", "Текущий режим и состояние служебной ветки.", "/panel → Реакции", "главный администратор", ("mode","topic","repair"), ("mode","topic","repair")),
    _spec("reaction.topic_name_prompt", "Реакции", "Название служебной ветки", "Как назвать ветку, куда будут отправляться отмеченные реакциями сообщения? Отправьте название одним сообщением.", "Создание, восстановление или переименование служебной ветки.", "/panel → Реакции", "главный администратор"),
    _spec("reaction.mode_subscriber_set", "Реакции", "Включён режим 1", "Режим 1 включён. Новые реакции администраторов на сообщения подписчиц будут отправляться подписчицам как уведомления.", "Подтверждение режима 1.", "/panel → Реакции", "главный администратор"),
    _spec("reaction.mode_service_set", "Реакции", "Включён режим 2", "Режим 2 включён. Новые реакции администраторов будут отправлять отмеченные сообщения в служебную ветку <b>{topic}</b>.", "Подтверждение режима 2.", "/panel → Реакции", "главный администратор", ("topic",), ("topic",)),
    _spec("reaction.topic_created", "Реакции", "Служебная ветка создана", "Я создала служебную ветку <b>{topic}</b> и включила режим 2.", "Создание или пересоздание service topic.", "/panel → Реакции", "главный администратор", ("topic",), ("topic",)),
    _spec("reaction.topic_renamed", "Реакции", "Служебная ветка переименована", "Я переименовала служебную ветку в <b>{topic}</b>.", "Успешное переименование service topic.", "/panel → Реакции", "главный администратор", ("topic",), ("topic",)),
    _spec("reaction.topic_failed", "Реакции", "Служебная ветка недоступна", "Я не смогла изменить служебную ветку. Проверьте права бота на управление темами и попробуйте пересоздать её из General.", "Ошибка create/edit service topic или необходимость ремонта.", "/panel → Реакции", "главный администратор"),
    _spec("reaction.subscriber_notification", "Реакции", "Уведомление подписчице", "Администратор отреагировал на ваше сообщение: {reaction}", "Режим 1: приватное уведомление о новой реакции.", "message_reaction", "подписчица", ("reaction",), ("reaction",)),
    _spec("reaction.service_anonymous_source", "Реакции", "Анонимный источник", "Источник: <b>{anonymous_tag}</b>.", "Privacy-safe подпись к сообщению, скопированному в служебную ветку в режиме 2.", "message_reaction", "администратор", ("anonymous_tag",), ("anonymous_tag",)),
)

_RUNTIME_UI_SPECS = (
    _spec("prestart.overview", "Карточка до Start", "Настройка карточки", "<b>Карточка до Start</b>\n\nЭта настройка общая для всего бота и не зависит от выбранного канала.\n\nТекущий текст:\n{description}\n\nМедиа: <b>{media_state}</b>.\nТекст применяется автоматически. Description Picture применяется через @BotFather из подготовленного здесь медиа.", "Экран управления общей карточкой до Start.", "/panel → Карточка до Start", "главный администратор", ("description", "media_state"), ("description", "media_state")),
    _spec("prestart.private_required", "Карточка до Start", "Только личный чат", "Редактирование карточки доступно только в личном чате с ботом.", "Отказ при попытке редактировать глобальную карточку вне private chat.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.text_prompt", "Карточка до Start", "Новый текст", "Отправьте новый текст карточки одним сообщением (до 512 символов).", "Запрос нового текста глобальной карточки.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_prompt", "Карточка до Start", "Новое медиа", "Отправьте одно фото, видео или GIF/анимацию для предпросмотра карточки.", "Запрос медиа предпросмотра.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_removed", "Карточка до Start", "Медиа удалено", "Сохранённое медиа удалено. Если оно уже применялось как Description Picture, удалите его также через @BotFather.", "Подтверждение удаления сохранённого медиа.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.reset_failed", "Карточка до Start", "Сброс не выполнен", "Я не смогла вернуть стандартный текст карточки.", "Ошибка применения стандартного описания через Bot API.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.reset_done", "Карточка до Start", "Карточка сброшена", "Стандартный текст восстановлен, сохранённое медиа удалено. Если Description Picture было применено, завершите сброс через @BotFather.", "Подтверждение полного сброса карточки.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.invalid_text", "Карточка до Start", "Некорректный текст", "Нужен непустой текст длиной до 512 символов.", "Ошибка валидации описания карточки.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.text_confirm", "Карточка до Start", "Подтвердить текст", "Применить этот текст к общей карточке бота?", "Подтверждение изменения описания.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_confirm", "Карточка до Start", "Подтвердить медиа", "Сохранить это медиа как подготовленный вариант Description Picture? После сохранения я дам кнопку перехода в @BotFather для фактического применения.", "Подтверждение сохранения media preview.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.stale", "Карточка до Start", "Действие устарело", "Действие устарело. Откройте настройки карточки заново.", "Stale callback/FSM карточки.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.cancelled", "Карточка до Start", "Изменение отменено", "Изменение карточки отменено.", "Отмена изменения карточки.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.apply_failed", "Карточка до Start", "Текст не применён", "Я не смогла применить текст карточки.", "Ошибка сохранения нового описания.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.applied", "Карточка до Start", "Текст применён", "Текст карточки применён.", "Подтверждение сохранения описания.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_stale", "Карточка до Start", "Медиа недоступно", "Медиа больше недоступно. Отправьте его заново.", "Потерянный media draft.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_saved", "Карточка до Start", "Медиа сохранено", "Медиа сохранено и подготовлено к применению через @BotFather.", "Подтверждение сохранения media preview.", "/panel → Карточка до Start", "главный администратор"),
    _spec("prestart.media_missing", "Карточка до Start", "Медиа не выбрано", "Сначала выберите и сохраните фото, видео или анимацию.", "Попытка подготовить Description Picture без сохранённого медиа.", "/panel → Карточка до Start", "главный администратор"),

    _spec("template_ui.category_page", "Тексты и оформление", "Страница категории", "<b>{category}</b>\nСтраница {page}", "Заголовок страницы категории редактора.", "/panel → Тексты", "главный администратор", ("category", "page"), ("category", "page")),
    _spec("template_ui.home", "Тексты и оформление", "Главная редактора", "<b>Тексты и оформление</b>\nВыберите категорию шаблонов.", "Главный экран редактора шаблонов.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.edit_prompt", "Тексты и оформление", "Введите новый текст", "Отправьте новый текст одним сообщением. Разрешённые переменные указаны в карточке шаблона.", "Начало редактирования шаблона.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.invalid_text", "Тексты и оформление", "Текст не принят", "Текст не сохранён: проверьте длину, переменные и формат.", "Ошибка валидации custom template.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.stale", "Тексты и оформление", "Действие устарело", "Действие устарело. Откройте редактор текстов заново.", "Stale callback/FSM редактора.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.invalid_saved", "Тексты и оформление", "Черновик недействителен", "Текст больше недействителен. Начните редактирование заново.", "Повторная validation перед сохранением.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.saved", "Тексты и оформление", "Текст сохранён", "Текст сохранён.", "Подтверждение channel override.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.reset_one_prompt", "Тексты и оформление", "Сбросить текст", "Вернуть стандартный текст?", "Подтверждение сброса одного override.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.reset_all_prompt", "Тексты и оформление", "Сбросить все тексты", "Сбросить все изменённые тексты этого канала?", "Подтверждение сброса всех overrides канала.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.cancelled", "Тексты и оформление", "Изменение отменено", "Изменение текстов отменено.", "Отмена editor FSM.", "/panel → Тексты", "главный администратор"),
    _spec("template_ui.reset_done", "Тексты и оформление", "Стандартные тексты восстановлены", "Стандартные тексты восстановлены.", "Подтверждение reset override(s).", "/panel → Тексты", "главный администратор"),

    _spec("subscriber.actions_prompt", "Подписчица", "Действия с подписчицей", "Вот что можно сделать с этой подписчицей:", "Меню действий /subscriber.", "/subscriber", "администратор"),
    _spec("subscriber.spam_unavailable", "Подписчица", "Спам-действие устарело", "Данные больше недоступны.", "Stale/forged callback отметки спама.", "/subscriber", "администратор"),
    _spec("subscriber.spam_updated", "Подписчица", "Метка спама обновлена", "Метка спама обновлена.", "Подтверждение mark/unmark spam.", "/subscriber", "администратор"),
    _spec("subscriber.history_empty", "Подписчица", "История ограничений пуста", "История ограничений пуста.", "Пустая moderation history.", "/subscriber_history", "администратор"),
    _spec("subscriber.history_title", "Подписчица", "История ограничений", "<b>История ограничений</b>", "Заголовок moderation history.", "/subscriber_history", "администратор"),
    _spec("channel.open_personal_link", "Выбор канала", "Нет активной предложки", "Сначала откройте бота по персональной ссылке из нужного Telegram-канала.", "Сообщение при попытке написать без активного канала.", "приём сообщения", "подписчица", scope="global"),
    _spec("reaction.topic_name_invalid", "Реакции", "Некорректное название ветки", "Название должно содержать от 1 до 128 символов.", "Ошибка проверки названия служебной ветки.", "/panel → Реакции", "главный администратор"),
    _spec(
        "sanction.flow.confirmation",
        "Ограничения и модерация",
        "Подтверждение санкции",
        "<b>Подтвердите санкцию</b>\n{target}\nДействие: {action}{parameter}\nПричина: {reason}\nПоказать подписчице: {visible}",
        "Финальное подтверждение moderation action перед применением.",
        "sanction FSM",
        "администратор",
        ("target", "action", "parameter", "reason", "visible"),
        ("target", "action", "reason", "visible"),
    )
)

_SPECS = _SPECS + _SETUP_SPECS + _CHANNEL_PRIVACY_SPECS + _STATUS_TOPIC_SPECS + _BROADCAST_SPECS + _REACTION_SPECS + _RUNTIME_UI_SPECS

TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {item.key: item for item in _SPECS}
if len(TEMPLATE_REGISTRY) != len(_SPECS):
    raise RuntimeError("Duplicate template key")


def categories() -> list[str]:
    return list(dict.fromkeys(spec.category for spec in _SPECS))


def specs_for_category(category: str) -> list[TemplateSpec]:
    return [spec for spec in _SPECS if spec.category == category]


def validate_template(key: str, text: str) -> None:
    spec = TEMPLATE_REGISTRY.get(key)
    if spec is None:
        raise ValueError("Unknown template")
    if not isinstance(text, str) or len(text) > MAX_TEMPLATE_LENGTH or (not text.strip() and not spec.allow_empty):
        raise ValueError("Template text is empty or too long")
    try:
        fields = list(Formatter().parse(text))
    except ValueError as exc:
        raise ValueError("Malformed template braces") from exc
    seen = set()
    for _, name, format_spec, conversion in fields:
        if name is None:
            continue
        if not name.isidentifier() or format_spec or conversion or name not in spec.variables:
            raise ValueError("Unsupported template placeholder")
        seen.add(name)
    if not spec.required.issubset(seen):
        raise ValueError("Required template placeholder is missing")


def render_default(key: str, values: Mapping[str, object]) -> str:
    spec = TEMPLATE_REGISTRY[key]
    safe = {name: html.escape(str(values.get(name, ""))) for name in spec.variables}
    return spec.default.format(**safe)


async def render_template(db, channel_id: int, key: str, **values: object) -> str:
    spec = TEMPLATE_REGISTRY[key]
    text = await db.get_template_override(channel_id=channel_id, template_key=key)
    if text is not None:
        try:
            validate_template(key, text)
            safe = {name: html.escape(str(values.get(name, ""))) for name in spec.variables}
            return text.format(**safe)
        except (ValueError, KeyError):
            logger.warning("Invalid template override ignored channel=%s key=%s", channel_id, key)
    return render_default(key, values)


def preview_values(spec: TemplateSpec) -> dict[str, str]:
    examples = {"channel_name": "Пример предложки", "anonymous_tag": "Анон-18", "status": "В работе", "reason": "Нарушение правил", "duration": "1 час", "expires_at": "14.08.2026 12:00", "count": "12", "admin_name": "Администратор", "action": "Ограничение", "prefix": "Анон", "next_number": "19", "name": "Ирина П.", "username": "@irina", "user_id": "123456789", "first_seen": "14.08.2026 10:00", "last_seen": "14.08.2026 12:30", "message_count": "7", "reaction": "👍", "mode": "Режим 1", "topic": "Важное", "repair": "Готово"}
    return {name: examples.get(name, "Пример") for name in spec.variables}
