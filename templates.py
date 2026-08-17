"""Channel-scoped Telegram UI template registry and safe renderer."""
from __future__ import annotations

import html
import logging
import re
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


# Human-readable dynamic-field names used by the owner-facing editor.
# Internal placeholder names remain stable for backwards compatibility and
# storage, but ordinary owners should never need to type them manually.
VARIABLE_LABELS: dict[str, str] = {
    "action": "Действие",
    "actions": "Действия",
    "allowed": "Разрешено",
    "anonymous_tag": "Анонимный номер",
    "basis": "Основа очистки",
    "body": "Содержимое",
    "category": "Категория",
    "channel_id": "ID предложки",
    "channel_name": "Название предложки",
    "closed": "Закрыто",
    "count": "Количество",
    "created": "Создано",
    "days": "Количество дней",
    "deep_link": "Персональная ссылка",
    "deleted": "Удалено",
    "delivered": "Доставлено",
    "description": "Описание",
    "display_name": "Отображаемое имя",
    "duration": "Срок",
    "enabled": "Включено",
    "error": "Ошибка",
    "errors": "Ошибки",
    "expires_at": "Действует до",
    "failed": "Ошибок",
    "final_delete_days": "Дней до удаления",
    "first_seen": "Первое обращение",
    "important": "Важное",
    "last_seen": "Последняя активность",
    "legacy_warning": "Предупреждение о старых данных",
    "media_state": "Состояние медиа",
    "message_count": "Количество сообщений",
    "mode": "Режим",
    "name": "Имя пользователя",
    "next_number": "Следующий анонимный номер",
    "next_reset": "Следующая очистка",
    "notice_text": "Текст уведомления",
    "page": "Номер страницы",
    "page_title": "Заголовок страницы",
    "pages": "Количество страниц",
    "parameter": "Параметр",
    "period": "Период",
    "period_days": "Период в днях",
    "pinned": "Закреплено",
    "prefix": "Анонимный префикс",
    "reaction": "Реакция",
    "revision_id": "Номер версии",
    "initial_revision_id": "Исходная версия",
    "standard_revision_id": "Версия стандарта",
    "source_name": "Название исходной предложки",
    "target_name": "Название целевой предложки",
    "title": "Заголовок операции",
    "active_revision_id": "Опубликованная версия",
    "created_at": "Дата создания",
    "actor": "Автор изменения",
    "source": "Источник версии",
    "summary": "Описание изменения",
    "item_count": "Количество элементов",
    "changed_count": "Количество изменений",
    "changes": "Изменённые элементы",
    "reason": "Причина",
    "repair": "Состояние восстановления",
    "scope": "Область",
    "schema_version": "Версия схемы",
    "skipped": "Пропущено",
    "status": "Статус",
    "subscribers": "Подписчики",
    "tag": "Тег",
    "target": "Получатель",
    "text": "Текст",
    "timezone": "Часовой пояс",
    "topic": "Название ветки",
    "topics": "Количество веток",
    "undelivered": "Не доставлено",
    "unique_recipients": "Уникальные получатели",
    "user_id": "Telegram ID пользователя",
    "username": "Username пользователя",
    "visible": "Показывать пользователю",
    "warning": "Предупреждение",
    "active_1d": "Активны за 1 день",
    "active_7d": "Активны за 7 дней",
    "active_30d": "Активны за 30 дней",
    "new_subscribers": "Новые подписчики",
    "subscriber_messages": "Сообщения подписчиков",
    "admin_replies": "Ответы администраторов",
    "average_messages_per_subscriber": "Среднее сообщений на подписчика",
    "conversation_count": "Количество обращений",
    "answered_count": "Обращения с ответом",
    "answered_share": "Доля обращений с ответом",
    "text_count": "Текстовые сообщения",
    "photo_count": "Фото",
    "video_count": "Видео",
    "document_count": "Документы",
    "voice_count": "Голосовые",
    "audio_count": "Аудио",
    "sticker_count": "Стикеры",
    "other_count": "Другое",
    "album_count": "Альбомы",
    "media_items_count": "Медиаэлементы",
    "average_first_response": "Среднее время первого ответа",
    "median_first_response": "Медиана первого ответа",
    "most_active_hour": "Самый активный час",
    "most_active_day": "Самый активный день",
    "top_hours": "Топ часов",
    "weekdays": "Активность по дням",
    "rows": "Строки",
    "active_admin_count": "Активные администраторы",
    "handled_conversations": "Обработанные обращения",
    "unanswered_conversations": "Обращения без ответа",
    "team_average_response": "Среднее время ответа команды",
    "team_median_response": "Медиана ответа команды",
    "top_reply_admin": "Лидер по ответам",
    "top_first_response_admin": "Лидер по первым ответам",
    "active_days": "Активные дни",
    "last_7_days": "Активность за 7 дней",
    "last_30_days": "Активность за 30 дней",
    "conversations": "Обращения",
    "answered_conversations": "Отвеченные обращения",
    "answered_percentage": "Процент отвеченных",
    "closed_conversations": "Закрытые обращения",
    "average_messages_per_conversation": "Среднее сообщений на обращение",
    "warnings": "Предупреждения",
    "restrictions": "Ограничения",
    "active_restrictions": "Активные ограничения",
    "notes": "Заметки",
    "tags": "Теги",
    "created_at": "Дата",
    "admin": "Администратор",
    "show_reason": "Показывать причину",
    "position": "Номер",
}


class TemplateValidationError(ValueError):
    """Structured validation error for human-friendly editor feedback."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        missing: tuple[str, ...] = (),
        length: int | None = None,
        limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.missing = missing
        self.length = length
        self.limit = limit


def variable_label(name: str) -> str:
    return VARIABLE_LABELS.get(name, name.replace("_", " ").strip().capitalize())


def friendly_placeholder(name: str) -> str:
    """Owner-facing token. It deliberately looks like prose, not source code."""
    return f"‹{variable_label(name)}›"


def template_field_rows(spec: TemplateSpec) -> list[tuple[str, str, str, bool]]:
    return [
        (name, variable_label(name), friendly_placeholder(name), name in spec.required)
        for name in sorted(spec.variables, key=lambda item: variable_label(item).casefold())
    ]


def normalize_editor_template(key: str, text: str) -> str:
    """Convert an owner-facing rich-text draft to the internal template form.

    The input is expected to be safe Telegram HTML generated from Message.html_text.
    Friendly tokens such as ``‹Название предложки›`` become the stable internal
    ``{channel_name}`` placeholder. Exact legacy placeholders are preserved for
    backwards compatibility. All other braces are escaped automatically so an
    ordinary owner can type literal braces without learning Formatter syntax.
    """
    spec = TEMPLATE_REGISTRY.get(key)
    if spec is None:
        raise TemplateValidationError("unknown_template", "Unknown template")
    if not isinstance(text, str):
        raise TemplateValidationError("not_text", "Template must be text")

    protected: dict[str, str] = {}

    def hold(value: str) -> str:
        marker = f"\ue000{len(protected)}\ue001"
        protected[marker] = value
        return marker

    # Preserve existing escaped literal braces exactly.
    text = text.replace("{{", hold("{{")).replace("}}", hold("}}"))

    # Friendly tokens are the normal UI. The [[Label]] form is accepted as a
    # compatibility/convenience alias for users who cannot easily type ‹ ›.
    for name, label, token, _required in template_field_rows(spec):
        for visible in (token, f"[[{label}]]"):
            text = text.replace(visible, hold("{" + name + "}"))

    # Preserve exact supported legacy placeholders for old power-user flows.
    for name in sorted(spec.variables, key=len, reverse=True):
        text = text.replace("{" + name + "}", hold("{" + name + "}"))

    # Any remaining braces are ordinary user text, not code.
    text = text.replace("{", "{{").replace("}", "}}")

    for marker, value in protected.items():
        text = text.replace(marker, value)
    return text


def validation_error_message(error: ValueError, *, key: str) -> str:
    """Return precise, safe HTML explaining why a draft was rejected."""
    if not isinstance(error, TemplateValidationError):
        return "<b>Текст не сохранён.</b> Проверьте сообщение и попробуйте ещё раз."
    if error.code == "empty":
        return "<b>Текст пустой.</b> Отправьте хотя бы один символ."
    if error.code == "too_long":
        length = error.length if error.length is not None else 0
        limit = error.limit if error.limit is not None else MAX_TEMPLATE_LENGTH
        return f"<b>Текст слишком длинный.</b> Сейчас {length} символов, максимум — {limit}."
    if error.code == "missing_required":
        names = [variable_label(name) for name in error.missing]
        items = "\n".join(f"• {html.escape(name)}" for name in names)
        return (
            "<b>Не хватает обязательных динамических полей.</b>\n"
            + items
            + "\n\nДобавьте их в нужное место текста через понятные метки из кнопок редактора."
        )
    if error.code == "unsupported_field":
        label = variable_label(error.field or "") if error.field else "неизвестное поле"
        return (
            "<b>Неизвестное динамическое поле.</b> "
            f"{html.escape(label)} не поддерживается в этом сообщении. "
            "Используйте только поля, показанные редактором."
        )
    if error.code == "field_format":
        return "<b>Некорректное динамическое поле.</b> Формат и преобразования для полей не поддерживаются."
    if error.code == "malformed_braces":
        return "<b>Не удалось разобрать динамическое поле.</b> Используйте метки, которые показывает редактор."
    if error.code == "unknown_template":
        return "<b>Этот шаблон больше недоступен.</b> Откройте редактор заново."
    return "<b>Текст не сохранён.</b> Проверьте сообщение и попробуйте ещё раз."


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
    _spec("panel.overview", "Панель", "Обзор панели", "<b>Панель предложки</b>\n\nГруппа: {channel_name}\nПодписчиков: <b>{subscribers}</b>\nАктивных тем: <b>{topics}</b>\n\nПериод автоочистки: <b>{period_days} дней</b>\nСледующий сброс: <b>{next_reset}</b>\n\n<b>Диплинк:</b>\n<code>{deep_link}</code>\n\n<b>Предупреждение за 24 часа:</b>\n{notice_text}\n\n<code>/set_period 30</code> — период\n<code>/set_announcement текст</code> — анонс\n<code>/set_timezone Europe/Moscow</code> — часовой пояс", "Главный экран /panel.", "/panel", "главный администратор", ("channel_name","subscribers","topics","period_days","timezone","next_reset","deep_link","notice_text"), ("channel_name","subscribers","topics","period_days","next_reset","deep_link","notice_text")),
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
    _spec("start_card.overview", "Стартовая карточка", "Оформление после Start", "<b>Стартовая карточка предложки</b>\n\nКанал: <b>{channel_name}</b>\nchannel_id: <code>{channel_id}</code>\n\nТекст берётся из приветствия после Start.\nМедиа: <b>{media_state}</b>.\n\nЭта карточка относится только к данной предложке и не меняет глобальный профиль бота.", "Экран channel-scoped оформления первого сообщения после Start.", "/panel → Стартовая карточка", "главный администратор", ("channel_name", "channel_id", "media_state"), ("channel_name", "channel_id", "media_state")),
    _spec("start_card.private_required", "Стартовая карточка", "Только личный чат", "Редактирование стартовой карточки выполняется в личном чате с ботом. Откройте /panel в личке и выберите эту предложку.", "Редактирование channel start card вне private chat.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.text_prompt", "Стартовая карточка", "Новый текст", "Отправьте новый текст приветствия одним обычным сообщением. Форматируйте его средствами Telegram; динамические поля можно добавить кнопками ниже.", "Запрос текста channel start card в редакторе без кода.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.invalid_text", "Стартовая карточка", "Некорректный текст", "Текст не сохранён. Проверьте длину, формат и обязательное поле «Название предложки».", "Ошибка текущего валидатора start.greeting.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.text_confirm", "Стартовая карточка", "Подтверждение текста", "Применить этот текст только к текущей предложке?", "Подтверждение изменения start.greeting.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.text_saved", "Стартовая карточка", "Текст сохранён", "Текст стартовой карточки этой предложки сохранён.", "Успешное сохранение start.greeting override.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_prompt", "Стартовая карточка", "Новое медиа", "Отправьте одно фото, видео или GIF/анимацию. Оно будет показываться только подписчикам этой предложки после Start.", "Запрос channel-scoped media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_confirm", "Стартовая карточка", "Подтверждение медиа", "Сохранить это медиа для стартовой карточки текущей предложки?", "Подтверждение channel media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_saved", "Стартовая карточка", "Медиа сохранено", "Медиа стартовой карточки сохранено только для этой предложки.", "Успешное сохранение channel media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_removed", "Стартовая карточка", "Медиа удалено", "Медиа стартовой карточки удалено. Текст приветствия сохранён.", "Удаление channel media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_missing", "Стартовая карточка", "Медиа не выбрано", "У этой предложки сейчас нет сохранённого медиа стартовой карточки.", "Попытка удалить отсутствующее media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.media_stale", "Стартовая карточка", "Медиа недоступно", "Сохранённое медиа больше недоступно в Telegram. Текст карточки продолжит работать; загрузите медиа заново.", "Stale Telegram file_id channel start card.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.preview_header", "Стартовая карточка", "Предпросмотр", "<b>Предпросмотр стартовой карточки</b>\nКанал: {channel_name}", "Заголовок безопасного owner preview.", "/panel → Стартовая карточка", "главный администратор", ("channel_name",), ("channel_name",)),
    _spec("start_card.cancelled", "Стартовая карточка", "Изменение отменено", "Изменение стартовой карточки отменено.", "Отмена FSM channel start card.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("start_card.stale", "Стартовая карточка", "Действие устарело", "Действие устарело. Откройте стартовую карточку заново через /panel.", "Stale callback/FSM channel start card.", "/panel → Стартовая карточка", "главный администратор"),

    _spec("prestart.overview", "Карточка до Start", "Настройка карточки", "<b>Карточка до Start</b>\n\nЭта настройка общая для всего бота и не зависит от выбранного канала.\n\nТекущий текст:\n{description}\n\nМедиа: <b>{media_state}</b>.\nТекст применяется автоматически. Description Picture применяется через @BotFather из подготовленного здесь медиа.", "Экран управления общей карточкой до Start.", "/panel → Карточка до Start", "главный администратор", ("description", "media_state"), ("description", "media_state"), scope="global"),
    _spec("prestart.private_required", "Карточка до Start", "Только личный чат", "Редактирование карточки доступно только в личном чате с ботом.", "Отказ при попытке редактировать глобальную карточку вне private chat.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.text_prompt", "Карточка до Start", "Новый текст", "Отправьте новый текст карточки одним сообщением (до 512 символов).", "Запрос нового текста глобальной карточки.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_prompt", "Карточка до Start", "Новое медиа", "Отправьте одно фото, видео или GIF/анимацию для предпросмотра карточки.", "Запрос медиа предпросмотра.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_removed", "Карточка до Start", "Медиа удалено", "Сохранённое медиа удалено. Если оно уже применялось как Description Picture, удалите его также через @BotFather.", "Подтверждение удаления сохранённого медиа.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.reset_failed", "Карточка до Start", "Сброс не выполнен", "Я не смогла вернуть стандартный текст карточки.", "Ошибка применения стандартного описания через Bot API.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.reset_done", "Карточка до Start", "Карточка сброшена", "Стандартный текст восстановлен, сохранённое медиа удалено. Если Description Picture было применено, завершите сброс через @BotFather.", "Подтверждение полного сброса карточки.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.invalid_text", "Карточка до Start", "Некорректный текст", "Нужен непустой текст длиной до 512 символов.", "Ошибка валидации описания карточки.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.text_confirm", "Карточка до Start", "Подтвердить текст", "Применить этот текст к общей карточке бота?", "Подтверждение изменения описания.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_confirm", "Карточка до Start", "Подтвердить медиа", "Сохранить это медиа как подготовленный вариант Description Picture? После сохранения я дам кнопку перехода в @BotFather для фактического применения.", "Подтверждение сохранения media preview.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.stale", "Карточка до Start", "Действие устарело", "Действие устарело. Откройте настройки карточки заново.", "Stale callback/FSM карточки.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.cancelled", "Карточка до Start", "Изменение отменено", "Изменение карточки отменено.", "Отмена изменения карточки.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.apply_failed", "Карточка до Start", "Текст не применён", "Я не смогла применить текст карточки.", "Ошибка сохранения нового описания.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.applied", "Карточка до Start", "Текст применён", "Текст карточки применён.", "Подтверждение сохранения описания.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_stale", "Карточка до Start", "Медиа недоступно", "Медиа больше недоступно. Отправьте его заново.", "Потерянный media draft.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_saved", "Карточка до Start", "Медиа сохранено", "Медиа сохранено и подготовлено к применению через @BotFather.", "Подтверждение сохранения media preview.", "/panel → Карточка до Start", "главный администратор", scope="global"),
    _spec("prestart.media_missing", "Карточка до Start", "Медиа не выбрано", "Сначала выберите и сохраните фото, видео или анимацию.", "Попытка подготовить Description Picture без сохранённого медиа.", "/panel → Карточка до Start", "главный администратор", scope="global"),

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
    _spec("template_ui.reset_done", "Тексты и оформление", "Сброс добавлен в черновик", "Сброс добавлен в черновик. Опубликуйте изменения, чтобы они стали видны подписчикам.", "Подтверждение staging reset в draft.", "/panel → Тексты", "главный администратор"),
    _spec("custom.draft_saved", "Тексты и оформление", "Изменение добавлено в черновик", "Изменение сохранено в черновик. Подписчики пока видят опубликованную версию.", "Подтверждение записи channel customization draft.", "/panel → Тексты", "главный администратор"),
    _spec("custom.draft_status", "Тексты и оформление", "Есть неопубликованные изменения", "<b>Неопубликованные изменения:</b> {count}.\nОни видны только в предпросмотре до публикации.", "Статус persistent draft.", "/panel", "главный администратор", ("count",), ("count",)),
    _spec("custom.publish_prompt", "Тексты и оформление", "Подтверждение публикации", "Опубликовать все изменения черновика ({count}) одной версией? После публикации их увидят подписчики этой предложки.", "Подтверждение atomic publish.", "/panel", "главный администратор", ("count",), ("count",)),
    _spec("custom.publish_done", "Тексты и оформление", "Черновик опубликован", "Изменения опубликованы. Создана новая версия №{revision_id}.", "Результат atomic publish.", "/panel", "главный администратор", ("revision_id",), ("revision_id",)),
    _spec("custom.publish_empty", "Тексты и оформление", "Черновик пуст", "Нет неопубликованных изменений.", "Попытка публикации пустого draft.", "/panel", "главный администратор"),
    _spec("custom.publish_conflict", "Тексты и оформление", "Черновик устарел", "Опубликованная версия изменилась после создания этого черновика. Отмените черновик и начните редактирование заново.", "Оптимистический конфликт base revision.", "/panel", "главный администратор"),
    _spec("custom.discard_prompt", "Тексты и оформление", "Удалить черновик", "Удалить все неопубликованные изменения ({count})? Опубликованная версия не изменится.", "Подтверждение discard draft.", "/panel", "главный администратор", ("count",), ("count",)),
    _spec("custom.discard_done", "Тексты и оформление", "Черновик удалён", "Неопубликованные изменения удалены. Опубликованная версия не изменялась.", "Результат discard draft.", "/panel", "главный администратор"),
    _spec("custom.history_overview", "История оформления", "История версий", "<b>История оформления</b>\nПредложка: <b>{channel_name}</b>\nОпубликованная версия: <b>№{active_revision_id}</b>\nВсего версий: {count}", "Сводка immutable revisions конкретной предложки.", "/panel → История изменений", "главный администратор", ("channel_name","active_revision_id","count"), ("channel_name","active_revision_id","count")),
    _spec("custom.history_empty", "История оформления", "История пуста", "Для этой предложки пока нет сохранённых версий оформления.", "Пустая история Channel Custom Pack.", "/panel → История изменений", "главный администратор"),
    _spec("custom.history_revision", "История оформления", "Карточка версии", "<b>Версия №{revision_id}</b>\nСтатус: {status}\nСоздана: {created_at}\nИсточник: {source}\nАвтор: {actor}\nЭлементов: {item_count}\nИзменений относительно предыдущей версии: {changed_count}\n\n{summary}", "Подробности одной immutable revision.", "/panel → История изменений", "главный администратор", ("revision_id","status","created_at","source","actor","item_count","changed_count","summary"), ("revision_id","status","created_at","source","actor","item_count","changed_count","summary")),
    _spec("custom.history_changes", "История оформления", "Изменённые элементы", "<b>Изменённые элементы:</b>\n{changes}", "Friendly diff revision относительно предыдущей.", "/panel → История изменений", "главный администратор", ("changes",), ("changes",)),
    _spec("custom.history_restore_prompt", "История оформления", "Подтверждение восстановления", "Восстановить версию №{revision_id} в новый черновик? Опубликованная версия пока не изменится.", "Подтверждение безопасного rollback-to-draft.", "/panel → История изменений", "главный администратор", ("revision_id",), ("revision_id",)),
    _spec("custom.history_restore_staged", "История оформления", "Версия восстановлена в черновик", "Версия №{revision_id} восстановлена в черновик. Подготовлено изменений: {count}. Пропущено несовместимых элементов: {skipped}. Проверьте предпросмотр и опубликуйте изменения вручную.", "Результат stage rollback.", "/panel → История изменений", "главный администратор", ("revision_id","count","skipped"), ("revision_id","count","skipped")),
    _spec("custom.history_restore_current", "История оформления", "Версия уже активна", "Эта версия уже опубликована и не требует восстановления.", "Попытка восстановить current active revision.", "/panel → История изменений", "главный администратор"),
    _spec("custom.history_draft_exists", "История оформления", "Сначала завершите текущий черновик", "У предложки уже есть неопубликованный черновик. Сначала опубликуйте или удалите его, затем повторите восстановление версии.", "Защита существующего draft от перезаписи rollback-операцией.", "/panel → История изменений", "главный администратор"),
    _spec("custom.history_unavailable", "История оформления", "Версия недоступна", "Эта версия больше недоступна или не относится к выбранной предложке.", "Stale/forged revision callback.", "/panel → История изменений", "главный администратор"),
    _spec("custom.audit_overview", "История оформления", "Журнал действий", "<b>Журнал изменений оформления</b>\nПредложка: <b>{channel_name}</b>\nЗаписей: {count}", "Channel-scoped customization audit log.", "/panel → История изменений", "главный администратор", ("channel_name","count"), ("channel_name","count")),
    _spec("custom.audit_empty", "История оформления", "Журнал пуст", "В журнале этой предложки пока нет событий.", "Пустой customization audit.", "/panel → История изменений", "главный администратор"),
    _spec("custom.audit_event", "История оформления", "Событие журнала", "{created_at} · {actor}\n{action}", "Одна строка channel customization audit.", "/panel → История изменений", "главный администратор", ("created_at","actor","action"), ("created_at","actor","action")),
    _spec("custom.tools_overview", "Управление оформлением", "Перенос и сброс оформления", "<b>Управление оформлением</b>\nПредложка: <b>{channel_name}</b>\nОпубликованная версия: №{active_revision_id}\nИсходная версия: №{initial_revision_id}\nАктуальный стандарт: №{standard_revision_id}\n\nВсе массовые операции сначала создают черновик и не меняют опубликованное оформление до отдельной публикации.", "Сводка reset/apply/copy инструментов Channel Custom Pack.", "/panel → Управление оформлением", "главный администратор", ("channel_name","active_revision_id","initial_revision_id","standard_revision_id"), ("channel_name","active_revision_id","initial_revision_id","standard_revision_id")),
    _spec("custom.tools_draft_exists", "Управление оформлением", "Сначала завершите черновик", "У предложки уже есть неопубликованные изменения. Сначала опубликуйте или удалите текущий черновик, затем запускайте массовый сброс, применение стандарта или копирование.", "Защита существующего draft от перезаписи bulk-операцией.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_no_changes", "Управление оформлением", "Изменений нет", "Выбранный источник уже совпадает с опубликованным оформлением этой предложки. Создавать черновик не требуется.", "Bulk-операция без различий.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_plan", "Управление оформлением", "Предпросмотр различий", "<b>{title}</b>\nБудет подготовлено изменений: {count}.\nНесовместимых элементов будет пропущено: {skipped}.\n\n<b>Изменится:</b>\n{changes}\n\nОпубликованная версия пока не изменится.", "Read-only diff перед bulk staging.", "/panel → Управление оформлением", "главный администратор", ("title","count","skipped","changes"), ("title","count","skipped","changes")),
    _spec("custom.tools_reset_initial_prompt", "Управление оформлением", "Вернуть исходный кастом", "Подготовить исходную версию этой предложки как новый черновик? Это вернёт channel-specific тексты и стартовое медиа к состоянию первого снимка, но ничего не опубликует автоматически.", "Подтверждение reset-to-initial draft.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_apply_standard_prompt", "Управление оформлением", "Применить актуальный стандарт", "Подготовить актуальный Standard Custom Pack №{standard_revision_id} как новый черновик этой предложки? Существующие другие предложки не изменятся.", "Подтверждение apply-current-standard draft.", "/panel → Управление оформлением", "главный администратор", ("standard_revision_id",), ("standard_revision_id",)),
    _spec("custom.tools_copy_choose", "Управление оформлением", "Скопировать из своей предложки", "Выберите другую свою предложку. Будет использована только её текущая опубликованная версия; чужие и неопубликованные кастомы недоступны.", "Выбор source channel для copy-to-draft.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_copy_none", "Управление оформлением", "Нет источника для копирования", "У этого владельца нет другой подключённой предложки, из которой можно скопировать оформление.", "Нет другого own channel.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_copy_prompt", "Управление оформлением", "Подтвердить копирование", "Подготовить опубликованное оформление «{source_name}» как черновик для «{target_name}»? Исходная предложка не изменится.", "Подтверждение copy own channel to target draft.", "/panel → Управление оформлением", "главный администратор", ("source_name","target_name"), ("source_name","target_name")),
    _spec("custom.tools_copy_unavailable", "Управление оформлением", "Источник недоступен", "Эту предложку нельзя использовать как источник. Разрешено копирование только между предложками одного и того же владельца.", "Stale/forged copy source callback.", "/panel → Управление оформлением", "главный администратор"),
    _spec("custom.tools_staged", "Управление оформлением", "Изменения подготовлены", "{title} подготовлено как черновик. Изменений: {count}. Пропущено несовместимых элементов: {skipped}. Проверьте предпросмотр и опубликуйте изменения вручную.", "Результат reset/apply/copy staging.", "/panel → Управление оформлением", "главный администратор", ("title","count","skipped"), ("title","count","skipped")),
    _spec("custom.transfer_overview", "Импорт и экспорт", "Импорт / экспорт кастома", "<b>Импорт / экспорт кастома</b>\nРедактируется: <b>{channel_name}</b>\nchannel_id: {channel_id}\nОпубликованная версия: №{revision_id}\n\nЭкспорт содержит только оформление этой предложки. Импорт всегда проходит проверку и сначала создаёт черновик.", "Главный экран безопасного JSON transfer Channel Custom Pack.", "/panel → Импорт / экспорт кастома", "главный администратор", ("channel_name","channel_id","revision_id"), ("channel_name","channel_id","revision_id")),
    _spec("custom.transfer_import_prompt", "Импорт и экспорт", "Отправьте JSON", "Отправьте JSON-файл кастома одним документом. Максимальный размер: 512 КБ.\n\nФайл не может менять владельца, channel_id, права, callback-команды или данные подписчиков. Если в нём есть Telegram media file_id, бот отдельно проверит его доступность.", "Начало import FSM.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("custom.transfer_invalid", "Импорт и экспорт", "Файл не принят", "Файл не импортирован: {error}", "Понятная ошибка schema/security/semantic validation.", "/panel → Импорт / экспорт кастома", "главный администратор", ("error",), ("error",)),
    _spec("custom.transfer_media_unavailable", "Импорт и экспорт", "Медиа недоступно", "Telegram media из файла недоступно этому боту. Повторно загрузите медиа в стартовую карточку или импортируйте файл без него. Черновик не создан.", "Ошибка проверки переносимости Telegram file_id.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("custom.transfer_plan", "Импорт и экспорт", "Проверка перед импортом", "<b>Файл проверен</b>\nИсточник: {source_name}\nВерсия источника: {revision_id}\nБудет изменений: {count}\nПропущено: {skipped}\nМедиа: {media_state}\n\n<b>Изменится:</b>\n{changes}\n\nПосле подтверждения изменения попадут только в черновик.", "Diff/preview before staging imported JSON.", "/panel → Импорт / экспорт кастома", "главный администратор", ("source_name","revision_id","count","skipped","media_state","changes"), ("source_name","revision_id","count","skipped","media_state","changes")),
    _spec("custom.transfer_no_changes", "Импорт и экспорт", "Изменений нет", "Импортируемый кастом уже совпадает с опубликованным оформлением этой предложки. Черновик не создан.", "Import diff без изменений.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("custom.transfer_draft_exists", "Импорт и экспорт", "Сначала завершите черновик", "У предложки уже есть неопубликованный черновик. Сначала опубликуйте или удалите его; импорт не перезаписывает существующую работу.", "Защита существующего draft при импорте.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("custom.transfer_staged", "Импорт и экспорт", "Импорт подготовлен", "Импорт проверен и сохранён в черновик. Подготовлено изменений: {count}. Пропущено: {skipped}. Опубликованная версия пока не менялась.", "Результат import-to-draft.", "/panel → Импорт / экспорт кастома", "главный администратор", ("count","skipped"), ("count","skipped")),
    _spec("custom.transfer_export_caption", "Импорт и экспорт", "Экспорт готов", "Экспорт Channel Custom Pack готов. schema_version={schema_version}. В файл не включены токены, права, подписчики, сообщения, модерация, статистика и приватные заметки.", "Caption к JSON export document.", "/panel → Импорт / экспорт кастома", "главный администратор", ("schema_version",), ("schema_version",)),
    _spec("custom.transfer_cancelled", "Импорт и экспорт", "Импорт отменён", "Импорт отменён. Опубликованное оформление и черновик не изменялись.", "Отмена import FSM.", "/panel → Импорт / экспорт кастома", "главный администратор"),

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


_STAGE6_SURFACE_SPECS = (
    # Safe button / label surface. These values never change callback_data,
    # command names or stored enum keys; only the human-readable presentation
    # is channel-scoped.
    _spec("ui.common.back", "Кнопки интерфейса", "Назад", "Назад", "Общая кнопка возврата.", "inline keyboard", "администратор"),
    _spec("ui.common.cancel", "Кнопки интерфейса", "Отмена", "Отмена", "Общая кнопка отмены.", "inline keyboard", "администратор"),
    _spec("ui.common.save", "Кнопки интерфейса", "Сохранить", "Сохранить", "Общая кнопка сохранения.", "inline keyboard", "администратор"),
    _spec("ui.common.apply", "Кнопки интерфейса", "Применить", "Применить", "Общая кнопка применения.", "inline keyboard", "администратор"),
    _spec("ui.common.confirm", "Кнопки интерфейса", "Подтвердить", "Подтвердить", "Общая кнопка подтверждения.", "inline keyboard", "администратор"),
    _spec("ui.common.preview", "Кнопки интерфейса", "Предпросмотр", "Предпросмотр", "Общая кнопка предпросмотра.", "inline keyboard", "администратор"),
    _spec("ui.common.edit", "Кнопки интерфейса", "Изменить", "Изменить", "Общая кнопка изменения.", "inline keyboard", "администратор"),
    _spec("ui.common.delete", "Кнопки интерфейса", "Удалить", "Удалить", "Общая кнопка удаления.", "inline keyboard", "администратор"),
    _spec("ui.common.yes", "Кнопки интерфейса", "Да", "Да", "Положительный ответ.", "inline keyboard", "администратор"),
    _spec("ui.common.no", "Кнопки интерфейса", "Нет", "Нет", "Отрицательный ответ.", "inline keyboard", "администратор"),

    _spec("ui.panel.overview", "Панель — кнопки", "Обзор", "Обзор", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.statistics", "Панель — кнопки", "Статистика", "Статистика", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.cleanup", "Панель — кнопки", "Автоочистка", "Автоочистка", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.notices", "Панель — кнопки", "Уведомления", "Уведомления", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.search", "Панель — кнопки", "Поиск", "Поиск", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.anonymous", "Панель — кнопки", "Анонимность", "Анонимность", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.texts", "Панель — кнопки", "Тексты", "Тексты", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.manual_cleanup", "Панель — кнопки", "Ручная очистка", "Ручная очистка", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.start_card", "Панель — кнопки", "Стартовая карточка", "Стартовая карточка", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.reactions", "Панель — кнопки", "Реакции", "Реакции", "Кнопка панели.", "/panel", "главный администратор"),
    _spec("ui.panel.refresh", "Панель — кнопки", "Обновить", "Обновить", "Кнопка панели.", "/panel", "главный администратор"),

    _spec("ui.cleanup.enable", "Автоочистка — кнопки", "Включить автоочистку", "Включить автоочистку", "Кнопка автоочистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.disable", "Автоочистка — кнопки", "Отключить автоочистку", "Отключить автоочистку", "Кнопка автоочистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.basis_created", "Автоочистка — кнопки", "По созданию", "По созданию", "Основа очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.basis_activity", "Автоочистка — кнопки", "По активности", "По активности", "Основа очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.scope_all", "Автоочистка — кнопки", "Все темы", "Все темы", "Область очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.scope_completed", "Автоочистка — кнопки", "Только завершённые", "Только завершённые", "Область очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_delete", "Автоочистка — кнопки", "Удалять", "Удалять", "Действие очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_close", "Автоочистка — кнопки", "Закрывать", "Закрывать", "Действие очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_close_delete", "Автоочистка — кнопки", "Закрыть и удалить", "Закрыть и удалить", "Действие очистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.period_7", "Автоочистка — кнопки", "7 дней", "7 дней", "Быстрый период.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.period_30", "Автоочистка — кнопки", "30 дней", "30 дней", "Быстрый период.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.period_90", "Автоочистка — кнопки", "90 дней", "90 дней", "Быстрый период.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.confirm", "Автоочистка — кнопки", "Подтвердить очистку", "Подтвердить очистку", "Подтверждение ручной очистки.", "/panel → Ручная очистка", "главный администратор"),
    _spec("ui.cleanup.confirm_reset", "Автоочистка — кнопки", "Очистить и сбросить нумерацию", "Очистить + сбросить нумерацию", "Очистка с нумерацией.", "/panel → Ручная очистка", "главный администратор"),
    _spec("ui.cleanup.state_enabled", "Автоочистка — подписи", "Включена", "Включена", "Состояние автоочистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.state_disabled", "Автоочистка — подписи", "Выключена", "Выключена", "Состояние автоочистки.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.basis_created_text", "Автоочистка — подписи", "По дате создания", "по дате создания", "Основа очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.basis_activity_text", "Автоочистка — подписи", "По последней активности", "по последней активности", "Основа очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.scope_all_text", "Автоочистка — подписи", "Все обращения", "все обращения", "Область очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.scope_completed_text", "Автоочистка — подписи", "Отвеченные и закрытые", "отвеченные и закрытые", "Область очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_delete_text", "Автоочистка — подписи", "Удалить", "удалить", "Действие очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_close_text", "Автоочистка — подписи", "Закрыть", "закрыть", "Действие очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),
    _spec("ui.cleanup.action_close_delete_text", "Автоочистка — подписи", "Закрыть, затем удалить", "закрыть, затем удалить", "Действие очистки в обзоре.", "/panel → Автоочистка", "главный администратор"),

    _spec("ui.anonymous.edit_prefix", "Анонимность — кнопки", "Изменить префикс", "Изменить префикс", "Изменение anonymous prefix.", "/panel → Анонимность", "главный администратор"),
    _spec("ui.reaction.mode1", "Реакции — кнопки", "Режим 1", "Режим 1", "Режим реакций.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.mode2", "Реакции — кнопки", "Режим 2", "Режим 2", "Режим реакций.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.rename", "Реакции — кнопки", "Переименовать ветку", "Переименовать ветку", "Изменение service topic.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.recreate", "Реакции — кнопки", "Пересоздать ветку", "Пересоздать ветку", "Изменение service topic.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.create", "Реакции — кнопки", "Создать служебную ветку", "Создать служебную ветку", "Создание service topic.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.state_ready", "Реакции — подписи", "Готово", "готово", "Состояние service topic.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.state_repair", "Реакции — подписи", "Требуется восстановление", "требуется восстановление", "Состояние service topic.", "/panel → Реакции", "главный администратор"),
    _spec("ui.reaction.topic_missing", "Реакции — подписи", "Ветка не создана", "не создана", "Состояние service topic.", "/panel → Реакции", "главный администратор"),

    _spec("ui.start_card.edit_text", "Стартовая карточка — кнопки", "Изменить текст", "Изменить текст", "Кнопка стартовой карточки.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("ui.start_card.replace_media", "Стартовая карточка — кнопки", "Заменить медиа", "Заменить медиа", "Кнопка стартовой карточки.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("ui.start_card.remove_media", "Стартовая карточка — кнопки", "Удалить медиа", "Удалить медиа", "Кнопка стартовой карточки.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("ui.start_card.media_saved", "Стартовая карточка — подписи", "Медиа сохранено", "сохранено", "Состояние channel media.", "/panel → Стартовая карточка", "главный администратор"),
    _spec("ui.start_card.media_none", "Стартовая карточка — подписи", "Медиа отсутствует", "нет", "Состояние channel media.", "/panel → Стартовая карточка", "главный администратор"),

    _spec("ui.broadcast.send", "Рассылка — кнопки", "Отправить", "Отправить", "Отправить broadcast.", "/broadcast", "главный администратор"),
    _spec("ui.broadcast.edit", "Рассылка — кнопки", "Редактировать", "Редактировать", "Заменить broadcast draft.", "/broadcast", "главный администратор"),
    _spec("ui.broadcast.resume", "Рассылка — кнопки", "Продолжить рассылку", "Продолжить рассылку", "Resume broadcast.", "/broadcast", "главный администратор"),

    _spec("ui.search.new", "Поиск — кнопки", "Новый поиск", "Новый поиск", "Начать новый поиск.", "/panel → Поиск", "главный администратор"),
    _spec("ui.search.result", "Поиск — кнопки", "Результат поиска", "Результат {position}", "Кнопка результата поиска.", "/panel → Поиск", "главный администратор", ("position",), ("position",)),
    _spec("ui.search.open", "Поиск — кнопки", "Открыть", "Открыть", "Открыть forum topic.", "/panel → Поиск", "главный администратор"),
    _spec("search.result_line", "Поиск", "Строка результата", "• {display_name}{status}", "Строка найденного подписчика.", "/panel → Поиск", "главный администратор", ("display_name", "status"), ("display_name",)),

    _spec("ui.status.new", "Статусы — кнопки", "Новое", "Новое", "Статус обращения.", "/status", "администратор"),
    _spec("ui.status.in_progress", "Статусы — кнопки", "В работе", "В работе", "Статус обращения.", "/status", "администратор"),
    _spec("ui.status.answered", "Статусы — кнопки", "Отвечено", "Отвечено", "Статус обращения.", "/status", "администратор"),
    _spec("ui.status.closed", "Статусы — кнопки", "Закрыто", "Закрыто", "Статус обращения.", "/status", "администратор"),
    _spec("ui.status.mark_important", "Статусы — кнопки", "Отметить важной", "Отметить важной", "Защита обращения.", "/status", "администратор"),
    _spec("ui.status.unmark_important", "Статусы — кнопки", "Снять важность", "Снять важность", "Защита обращения.", "/status", "администратор"),
    _spec("ui.status.protect", "Статусы — кнопки", "Защитить от очистки", "Защитить от очистки", "Защита обращения.", "/status", "администратор"),
    _spec("ui.status.unprotect", "Статусы — кнопки", "Снять защиту от очистки", "Снять защиту от очистки", "Защита обращения.", "/status", "администратор"),

    _spec("ui.subscriber.rate_limit", "Подписчица — кнопки", "Rate-limit", "Rate-limit", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.mute", "Подписчица — кнопки", "Mute", "Mute", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.temporary_block", "Подписчица — кнопки", "Временная блокировка", "Временная блокировка", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.permanent_block", "Подписчица — кнопки", "Постоянная блокировка", "Постоянная блокировка", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.warning", "Подписчица — кнопки", "Предупреждение", "Предупреждение", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.clear", "Подписчица — кнопки", "Снять ограничения", "Снять ограничения", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.add_note", "Подписчица — кнопки", "Добавить заметку", "Добавить заметку", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.add_tag", "Подписчица — кнопки", "Добавить тег", "Добавить тег", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.notes", "Подписчица — кнопки", "Заметки", "Заметки", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.tags", "Подписчица — кнопки", "Теги", "Теги", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.statistics", "Подписчица — кнопки", "Статистика", "Статистика", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.history", "Подписчица — кнопки", "История ограничений", "История ограничений", "Действие с подписчицей.", "/subscriber", "администратор"),
    _spec("ui.subscriber.details", "Подписчица — кнопки", "Подробнее", "Подробнее {position}", "Кнопка history detail.", "/subscriber", "администратор", ("position",), ("position",)),
    _spec("ui.template.reset_all", "Редактор — кнопки", "Сбросить все тексты", "Сбросить все тексты", "Кнопка редактора текстов.", "/panel → Тексты", "главный администратор"),
    _spec("ui.template.reset_one", "Редактор — кнопки", "Вернуть стандартный", "Вернуть стандартный", "Кнопка сброса одного текста.", "/panel → Тексты", "главный администратор"),
    _spec("ui.template.state_changed", "Редактор — подписи", "Изменён", "изменён", "Состояние шаблона.", "/panel → Тексты", "главный администратор"),
    _spec("ui.template.state_standard", "Редактор — подписи", "Опубликован", "опубликован", "Состояние шаблона.", "/panel → Тексты", "главный администратор"),
    _spec("ui.template.state_draft", "Редактор — подписи", "Черновик", "есть черновик", "Состояние шаблона с неопубликованной правкой.", "/panel → Тексты", "главный администратор"),
    _spec("ui.custom.save_draft", "Редактор — кнопки", "Сохранить в черновик", "Сохранить в черновик", "Кнопка staging изменения.", "/panel", "главный администратор"),
    _spec("ui.custom.publish", "Редактор — кнопки", "Опубликовать изменения", "Опубликовать изменения", "Кнопка atomic publish channel draft.", "/panel", "главный администратор"),
    _spec("ui.custom.discard", "Редактор — кнопки", "Удалить черновик", "Удалить черновик", "Кнопка discard channel draft.", "/panel", "главный администратор"),
    _spec("ui.panel.history", "Панель — кнопки", "История изменений", "История изменений", "Переход к revisions и audit Channel Custom Pack.", "/panel", "главный администратор"),
    _spec("ui.custom.revisions", "История — кнопки", "Версии", "Версии", "Переход к истории immutable revisions.", "/panel → История изменений", "главный администратор"),
    _spec("ui.custom.audit", "История — кнопки", "Журнал действий", "Журнал действий", "Переход к customization audit log.", "/panel → История изменений", "главный администратор"),
    _spec("ui.custom.restore", "История — кнопки", "Восстановить в черновик", "Восстановить в черновик", "Безопасный rollback выбранной revision в draft.", "/panel → История изменений", "главный администратор"),
    _spec("ui.custom.preview_revision", "История — кнопки", "Предпросмотр версии", "Предпросмотр версии", "Предпросмотр исторической Channel Start Card.", "/panel → История изменений", "главный администратор"),
    _spec("ui.panel.custom_tools", "Панель — кнопки", "Управление оформлением", "Управление оформлением", "Reset/apply/copy инструменты Channel Custom Pack.", "/panel", "главный администратор"),
    _spec("ui.panel.custom_transfer", "Панель — кнопки", "Импорт / экспорт кастома", "Импорт / экспорт кастома", "Безопасный versioned JSON transfer Channel Custom Pack.", "/panel", "главный администратор"),
    _spec("ui.custom.export_json", "Импорт и экспорт — кнопки", "Экспорт JSON", "Экспортировать JSON", "Скачать опубликованный Channel Custom Pack.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("ui.custom.import_json", "Импорт и экспорт — кнопки", "Импорт JSON", "Импортировать JSON", "Начать проверяемый import-to-draft.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("ui.custom.confirm_import", "Импорт и экспорт — кнопки", "Создать черновик", "Создать черновик из файла", "Подтвердить import-to-draft после diff.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("ui.custom.cancel_import", "Импорт и экспорт — кнопки", "Отменить импорт", "Отменить импорт", "Отменить import FSM без side effects.", "/panel → Импорт / экспорт кастома", "главный администратор"),
    _spec("ui.custom.reset_initial", "Управление оформлением — кнопки", "Вернуть исходный кастом", "Вернуть исходный кастом", "Создать draft из initial channel snapshot.", "/panel → Управление оформлением", "главный администратор"),
    _spec("ui.custom.apply_standard", "Управление оформлением — кнопки", "Применить актуальный стандарт", "Применить актуальный стандарт", "Создать draft из current Standard Custom Pack.", "/panel → Управление оформлением", "главный администратор"),
    _spec("ui.custom.copy_from_channel", "Управление оформлением — кнопки", "Скопировать из другой своей предложки", "Скопировать из другой своей предложки", "Выбор own source channel для copy-to-draft.", "/panel → Управление оформлением", "главный администратор"),
    _spec("ui.common.previous", "Кнопки интерфейса", "Назад по страницам", "← Назад", "Общая кнопка предыдущей страницы.", "inline keyboard", "администратор"),
    _spec("ui.common.next", "Кнопки интерфейса", "Далее", "Далее →", "Общая кнопка следующей страницы.", "inline keyboard", "администратор"),
    _spec("ui.metadata.open_note", "Подписчица — кнопки", "Открыть заметку", "Открыть заметку {position}", "Кнопка заметки.", "/subscriber", "администратор", ("position",), ("position",)),
    _spec("ui.metadata.delete_tag", "Подписчица — кнопки", "Удалить тег", "Удалить тег {position}", "Кнопка тега.", "/subscriber", "администратор", ("position",), ("position",)),
    _spec("ui.metadata.to_notes", "Подписчица — кнопки", "К заметкам", "К заметкам", "Возврат к заметкам.", "/subscriber", "администратор"),
    _spec("subscriber.metadata.target_anonymous", "Подписчица", "Анонимная подписчица", "Анонимная подписчица: {anonymous_tag}", "Отображаемая цель metadata flow в anonymous mode.", "/subscriber", "администратор", ("anonymous_tag",), ("anonymous_tag",)),
    _spec("subscriber.metadata.target_identified", "Подписчица", "Идентифицированная подписчица", "Подписчица #{user_id}", "Отображаемая цель metadata flow в identified mode.", "/subscriber", "администратор", ("user_id",), ("user_id",)),
    _spec("ui.privacy.anonymous", "Приватность — подписи", "Анонимно", "Анонимно", "Название режима приватности.", "/privacy", "подписчица"),
    _spec("ui.privacy.identified", "Приватность — подписи", "Открыто", "Открыто", "Название режима приватности.", "/privacy", "подписчица"),

    _spec("ui.sanction.action.rate_limit", "Ограничения — подписи", "Rate-limit", "Rate-limit", "Название санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.mute", "Ограничения — подписи", "Mute", "Mute", "Название санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.temporary_block", "Ограничения — подписи", "Временная блокировка", "Временная блокировка", "Название санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.permanent_block", "Ограничения — подписи", "Постоянная блокировка", "Постоянная блокировка", "Название санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.warning", "Ограничения — подписи", "Предупреждение", "Предупреждение", "Название санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.clear_restrictions", "Ограничения — подписи", "Снятие ограничений", "Снятие ограничений", "Название действия.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.mark_spam", "Ограничения — подписи", "Спам", "Спам", "Название действия.", "sanction flow", "администратор"),
    _spec("ui.sanction.action.unmark_spam", "Ограничения — подписи", "Снятие спама", "Снятие спама", "Название действия.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.spam", "Ограничения — причины", "Спам", "Спам", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.flood", "Ограничения — причины", "Флуд", "Флуд", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.insult", "Ограничения — причины", "Оскорбления", "Оскорбления", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.rules", "Ограничения — причины", "Нарушение правил", "Нарушение правил", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.advertising", "Ограничения — причины", "Реклама", "Реклама", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.suspicious_activity", "Ограничения — причины", "Подозрительная активность", "Подозрительная активность", "Причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.reason.other", "Ограничения — причины", "Другое", "Другое", "Своя причина санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.other_interval", "Ограничения — кнопки", "Другой интервал", "Другой интервал", "Свой срок санкции.", "sanction flow", "администратор"),
    _spec("ui.sanction.status.active", "Ограничения — подписи", "Активно", "активно", "Статус санкции.", "sanction history", "администратор"),
    _spec("ui.sanction.status.expired", "Ограничения — подписи", "Истекло", "истекло", "Статус санкции.", "sanction history", "администратор"),
    _spec("ui.sanction.status.removed", "Ограничения — подписи", "Снято", "снято", "Статус санкции.", "sanction history", "администратор"),
    _spec("ui.sanction.status.warning", "Ограничения — подписи", "Выдано", "выдано", "Статус санкции.", "sanction history", "администратор"),
    _spec("ui.sanction.status.historical", "Ограничения — подписи", "История", "история", "Статус санкции.", "sanction history", "администратор"),
    _spec("ui.sanction.target_anonymous", "Ограничения — подписи", "Анонимная подписчица", "Анонимная подписчица: {anonymous_tag}", "Получатель санкции в анонимном режиме.", "sanction flow", "администратор", ("anonymous_tag",), ("anonymous_tag",)),
    _spec("ui.sanction.target_identified", "Ограничения — подписи", "Подписчица", "Подписчица #{user_id}", "Получатель санкции в открытом режиме.", "sanction flow", "администратор", ("user_id",), ("user_id",)),
    _spec("ui.sanction.parameter_duration", "Ограничения — подписи", "Срок или интервал", "Срок/интервал: {duration}", "Параметр санкции.", "sanction flow", "администратор", ("duration",), ("duration",)),
    _spec("ui.sanction.duration_permanent", "Ограничения — подписи", "Бессрочно", "Доступ ограничен бессрочно.", "Срок постоянной блокировки.", "sanction flow", "подписчица"),
    _spec("ui.sanction.duration_until", "Ограничения — подписи", "До даты", "До: {expires_at}.", "Срок временной санкции.", "sanction flow", "подписчица", ("expires_at",), ("expires_at",)),
    _spec("ui.sanction.admin", "Ограничения — подписи", "Администратор", "Администратор", "Источник moderation action.", "sanction history", "администратор"),
    _spec("ui.sanction.system", "Ограничения — подписи", "Системное действие", "Системное действие", "Источник системного moderation action.", "sanction history", "администратор"),

    _spec("ui.statistics.page.overview", "Статистика — подписи", "Обзор", "Обзор", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.page.messages", "Статистика — подписи", "Сообщения", "Сообщения", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.page.responses", "Статистика — подписи", "Ответы", "Ответы", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.page.activity", "Статистика — подписи", "Активность", "Активность", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.page.top", "Статистика — подписи", "Топ подписчиц", "Топ подписчиц", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.page.admins", "Статистика — подписи", "Администраторы", "Администраторы", "Название страницы статистики.", "/stats", "администратор"),
    _spec("ui.statistics.period.today", "Статистика — подписи", "Сегодня", "Сегодня", "Название периода статистики.", "/stats", "администратор"),
    _spec("ui.statistics.period.7d", "Статистика — подписи", "7 дней", "7 дней", "Название периода статистики.", "/stats", "администратор"),
    _spec("ui.statistics.period.30d", "Статистика — подписи", "30 дней", "30 дней", "Название периода статистики.", "/stats", "администратор"),
    _spec("ui.statistics.period.all", "Статистика — подписи", "Всё время", "Всё время", "Название периода статистики.", "/stats", "администратор"),
    _spec("ui.statistics.export", "Статистика — кнопки", "Экспорт", "Экспорт", "Кнопка экспорта.", "/panel → Статистика", "главный администратор"),

    _spec("statistics.body.overview", "Статистика", "Содержимое обзора", "Уникальные подписчицы: <b>{unique_recipients}</b>\nАктивны: 1д — {active_1d}; 7д — {active_7d}; 30д — {active_30d}\nНовые за период: <b>{new_subscribers}</b>\nСообщения подписчиц: <b>{subscriber_messages}</b>\nОтветы администраторов: <b>{admin_replies}</b>\nСреднее сообщений на подписчицу: {average_messages_per_subscriber}\nОбращения: {conversation_count}; С ответом: {answered_count} ({answered_share}%)", "Основные показатели канала.", "/stats", "администратор", ("unique_recipients","active_1d","active_7d","active_30d","new_subscribers","subscriber_messages","admin_replies","average_messages_per_subscriber","conversation_count","answered_count","answered_share"), ("unique_recipients","subscriber_messages")),
    _spec("statistics.body.messages", "Статистика", "Содержимое сообщений", "Текст: {text_count}\nФото: {photo_count}\nВидео: {video_count}\nДокументы: {document_count}\nГолосовые: {voice_count}\nАудио: {audio_count}\nСтикеры: {sticker_count}\nДругое: {other_count}\n\nАльбомы: <b>{album_count}</b>\nМедиаэлементов: <b>{media_items_count}</b>", "Разбивка типов сообщений.", "/stats", "администратор", ("text_count","photo_count","video_count","document_count","voice_count","audio_count","sticker_count","other_count","album_count","media_items_count"), ("text_count",)),
    _spec("statistics.body.responses", "Статистика", "Содержимое ответов", "Обращения: <b>{conversation_count}</b>\nС ответом: <b>{answered_count}</b>\nДоля отвеченных: <b>{answered_share}%</b>\n\nСреднее время первого ответа: <b>{average_first_response}</b>\nМедиана времени первого ответа: <b>{median_first_response}</b>", "Метрики ответа.", "/stats", "администратор", ("conversation_count","answered_count","answered_share","average_first_response","median_first_response"), ("conversation_count",)),
    _spec("statistics.body.activity", "Статистика", "Содержимое активности", "Самый активный час: <b>{most_active_hour}</b>\nСамый активный день: <b>{most_active_day}</b>\n\nТоп часов:\n{top_hours}\n\nПо дням недели:\n{weekdays}", "Активность по времени.", "/stats", "администратор", ("most_active_hour","most_active_day","top_hours","weekdays"), ("top_hours","weekdays")),
    _spec("statistics.body.top", "Статистика", "Содержимое топа", "{rows}", "Топ подписчиков.", "/stats", "администратор", ("rows",), ("rows",)),
    _spec("statistics.body.admins", "Статистика", "Содержимое администраторов", "Активных: {active_admin_count}; ответов: {admin_replies}\nОбработано обращений: <b>{handled_conversations}</b>; без ответа: <b>{unanswered_conversations}</b>\nСредний ответ: {team_average_response}; медиана: {team_median_response}\nЛидер по ответам: {top_reply_admin}; по первым ответам: {top_first_response_admin}\n\n{rows}", "Сводка работы администраторов.", "/stats", "администратор", ("active_admin_count","admin_replies","handled_conversations","unanswered_conversations","team_average_response","team_median_response","top_reply_admin","top_first_response_admin","rows"), ("active_admin_count",)),

    _spec("subscriber.statistics", "Подписчица", "Статистика подписчицы", "<b>Статистика</b>\n{target}\n\nСообщения: <b>{subscriber_messages}</b>; ответы: <b>{admin_replies}</b>\nАктивных дней: {active_days}; 7д: {last_7_days}; 30д: {last_30_days}\nОбращения: {conversations}; отвечено: {answered_conversations} ({answered_percentage}%); закрыто: {closed_conversations}\nСреднее сообщений/обращение: {average_messages_per_conversation}\nПервый ответ: среднее {average_first_response}, медиана {median_first_response}\nМедиа: текст {text_count}, фото {photo_count}, видео {video_count}, документы {document_count}, голосовые {voice_count}, аудио {audio_count}, стикеры {sticker_count}, другое {other_count}\nМодерация: предупреждения {warnings}, ограничения {restrictions}, активные {active_restrictions}, заметки {notes}, теги {tags}", "Полная статистика одной подписчицы.", "/subscriber", "администратор", ("target","subscriber_messages","admin_replies","active_days","last_7_days","last_30_days","conversations","answered_conversations","answered_percentage","closed_conversations","average_messages_per_conversation","average_first_response","median_first_response","text_count","photo_count","video_count","document_count","voice_count","audio_count","sticker_count","other_count","warnings","restrictions","active_restrictions","notes","tags"), ("target","subscriber_messages")),
    _spec("subscriber.history.page", "Подписчица", "Страница истории ограничений", "<b>История ограничений</b>\n{target}\nСтраница {page} из {pages}", "Заголовок страницы moderation history.", "/subscriber_history", "администратор", ("target","page","pages"), ("target","page","pages")),
    _spec("subscriber.history.entry", "Подписчица", "Строка истории ограничений", "• {action} — {created_at}; {status}{reason}", "Одна строка moderation history.", "/subscriber_history", "администратор", ("action","created_at","status","reason"), ("action","created_at","status")),
    _spec("subscriber.history.detail", "Подписчица", "Детали ограничения", "<b>{action}</b>\n{target}\nСтатус: {status}\nАдминистратор: {admin}\nПричина: {reason}\nПричина подписчице: {show_reason}\nИстекает: {expires_at}", "Детали moderation history.", "/subscriber_history", "администратор", ("action","target","status","admin","reason","show_reason","expires_at"), ("action","target","status")),
    _spec("subscriber.history.simple_entry", "Подписчица", "Строка краткой истории", "• <code>{created_at}</code>: {action}{reason}", "Краткий вывод /subscriber_history.", "/subscriber_history", "администратор", ("created_at","action","reason"), ("created_at","action")),
    _spec("subscriber.history.reason_suffix", "Подписчица", "Причина в строке истории", " ; причина: {reason}", "Суффикс причины в истории.", "/subscriber_history", "администратор", ("reason",), ("reason",)),
    _spec("subscriber.history.simple_reason_suffix", "Подписчица", "Причина в краткой истории", " — {reason}", "Суффикс причины в кратком /subscriber_history.", "/subscriber_history", "администратор", ("reason",), ("reason",)),
    _spec("subscriber.history.no_rows", "Подписчица", "Нет записей истории", "Записей пока нет.", "Пустая страница moderation history.", "/subscriber_history", "администратор"),
)

_SPECS = _SPECS + _SETUP_SPECS + _CHANNEL_PRIVACY_SPECS + _STATUS_TOPIC_SPECS + _BROADCAST_SPECS + _REACTION_SPECS + _RUNTIME_UI_SPECS + _STAGE6_SURFACE_SPECS

TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {item.key: item for item in _SPECS}
if len(TEMPLATE_REGISTRY) != len(_SPECS):
    raise RuntimeError("Duplicate template key")


def categories() -> list[str]:
    return list(dict.fromkeys(spec.category for spec in _SPECS))


def specs_for_category(category: str) -> list[TemplateSpec]:
    return [spec for spec in _SPECS if spec.category == category]


def channel_categories() -> list[str]:
    """Categories exposed to a CHANNEL_OWNER. Global bot/UI specs stay hidden."""
    return list(dict.fromkeys(spec.category for spec in _SPECS if spec.scope == "channel"))


def channel_specs_for_category(category: str) -> list[TemplateSpec]:
    return [spec for spec in _SPECS if spec.category == category and spec.scope == "channel"]


_TAG_RE = re.compile(r"<[^>]*>")


def plain_button_text(value: str, *, fallback: str) -> str:
    """Normalize a customizable label for Telegram inline buttons.

    Telegram button text does not parse HTML. Owners may still paste formatted
    text into the generic editor, so strip tags and control newlines here while
    keeping callback identifiers immutable.
    """
    clean = html.unescape(_TAG_RE.sub("", value)).replace("\n", " ").strip()
    return (clean or fallback)[:64]


async def render_label(
    db, channel_id: int, key: str, *, include_draft: bool = False, **values: object
) -> str:
    default = render_default(key, values)
    return plain_button_text(
        await render_template(
            db, channel_id, key, include_draft=include_draft, **values
        ),
        fallback=default,
    )


def validate_template(key: str, text: str) -> None:
    spec = TEMPLATE_REGISTRY.get(key)
    if spec is None:
        raise TemplateValidationError("unknown_template", "Unknown template")
    if not isinstance(text, str):
        raise TemplateValidationError("not_text", "Template must be text")
    if len(text) > MAX_TEMPLATE_LENGTH:
        raise TemplateValidationError(
            "too_long", "Template text is too long",
            length=len(text), limit=MAX_TEMPLATE_LENGTH,
        )
    if not text.strip() and not spec.allow_empty:
        raise TemplateValidationError("empty", "Template text is empty")
    try:
        fields = list(Formatter().parse(text))
    except ValueError as exc:
        raise TemplateValidationError("malformed_braces", "Malformed template braces") from exc
    seen = set()
    for _, name, format_spec, conversion in fields:
        if name is None:
            continue
        if format_spec or conversion:
            raise TemplateValidationError(
                "field_format", "Template placeholder formatting is unsupported", field=name
            )
        if not name.isidentifier() or name not in spec.variables:
            raise TemplateValidationError(
                "unsupported_field", "Unsupported template placeholder", field=name
            )
        seen.add(name)
    missing = tuple(sorted(spec.required.difference(seen)))
    if missing:
        raise TemplateValidationError(
            "missing_required", "Required template placeholder is missing", missing=missing
        )


def render_default(key: str, values: Mapping[str, object]) -> str:
    spec = TEMPLATE_REGISTRY[key]
    safe = {name: html.escape(str(values.get(name, ""))) for name in spec.variables}
    return spec.default.format(**safe)


async def render_template(
    db, channel_id: int, key: str, *, include_draft: bool = False, **values: object
) -> str:
    """Render a published channel snapshot, optionally overlaid by its draft.

    Normal runtime calls leave ``include_draft=False`` so subscribers never see
    unpublished owner edits. Preview surfaces opt in explicitly. The legacy
    override table is only a compatibility fallback for pre-v26 fixtures.
    """
    spec = TEMPLATE_REGISTRY[key]
    text: str | None = None
    if spec.scope == "channel":
        get_snapshot = getattr(db, "get_channel_custom_template_text", None)
        if get_snapshot is not None:
            try:
                text = await get_snapshot(
                    channel_id=channel_id, template_key=key,
                    include_legacy_template_overlay=True,
                    include_draft=include_draft,
                )
            except TypeError:
                # Compatibility with minimal test doubles / earlier DB APIs.
                text = await get_snapshot(
                    channel_id=channel_id, template_key=key,
                    include_legacy_template_overlay=True,
                )
            except (AttributeError, RuntimeError):
                text = None
        if text is None:
            get_override = getattr(db, "get_template_override", None)
            if get_override is not None:
                text = await get_override(channel_id=channel_id, template_key=key)
    if text is None:
        text = spec.default
    try:
        validate_template(key, text)
        safe = {name: html.escape(str(values.get(name, ""))) for name in spec.variables}
        return text.format(**safe)
    except (ValueError, KeyError):
        logger.warning("Invalid effective template ignored channel=%s key=%s", channel_id, key)
        return render_default(key, values)


def preview_values(spec: TemplateSpec) -> dict[str, str]:
    examples = {"channel_name": "Пример предложки", "anonymous_tag": "Анон-18", "status": "В работе", "reason": "Нарушение правил", "duration": "1 час", "expires_at": "14.08.2026 12:00", "count": "12", "admin_name": "Администратор", "action": "Ограничение", "prefix": "Анон", "next_number": "19", "name": "Ирина П.", "username": "@irina", "user_id": "123456789", "first_seen": "14.08.2026 10:00", "last_seen": "14.08.2026 12:30", "message_count": "7", "reaction": "👍", "mode": "Режим 1", "topic": "Важное", "repair": "Готово"}
    return {name: examples.get(name, "Пример") for name in spec.variables}
