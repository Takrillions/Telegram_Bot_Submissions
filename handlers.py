import asyncio
import html
import logging
from string import Formatter
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Hashable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
    BufferedInputFile,
    MessageReactionUpdated,
)
from aiogram.utils.deep_linking import create_start_link

from authorization import ChannelAction, ChannelAuthorizer
from broadcast_runtime import BroadcastRuntime
from reaction_runtime import ReactionRuntime
from command_menu import sync_command_menus
from export_runtime import csv_export, xlsx_export
from prestart_card import (
    BOTFATHER_URL,
    DEFAULT_PRESTART_DESCRIPTION,
    apply_description,
    description_picture_apply_instructions,
    description_picture_remove_instructions,
    validate_description,
    validate_media,
)
from templates import TEMPLATE_REGISTRY, categories as template_categories, preview_values, render_default, render_template, specs_for_category, validate_template
from database import Database, SANCTION_ACTIONS, SANCTION_REASON_CHOICES, SANCTION_REASON_LABELS, dt_from_db, utc_now

logger = logging.getLogger(__name__)

ACCESS_DENIED_TEXT = "У вас нет доступа к этой команде."


class SanctionFlow(StatesGroup):
    action = State()
    parameters = State()
    reason = State()
    custom_reason = State()
    visibility = State()
    confirmation = State()


class SearchFlow(StatesGroup):
    query = State()


class TemplateFlow(StatesGroup):
    edit = State()
    confirmation = State()
    reset_one = State()
    reset_all = State()


class SetupFlow(StatesGroup):
    anonymous_prefix = State()


class ChannelSettingsFlow(StatesGroup):
    anonymous_prefix = State()


class PreStartCardFlow(StatesGroup):
    description = State()
    description_confirmation = State()
    media = State()
    media_confirmation = State()


class BroadcastFlow(StatesGroup):
    message = State()
    confirmation = State()


class ReactionSettingsFlow(StatesGroup):
    topic_name = State()


class SubscriberMetadataFlow(StatesGroup):
    note = State()
    tag = State()
    note_edit = State()
    note_edit_confirmation = State()
    note_delete_confirmation = State()
    tag_delete_confirmation = State()


RATE_LIMIT_SECONDS = frozenset({5 * 60, 15 * 60, 30 * 60, 60 * 60})
DURATION_SECONDS = frozenset({10 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60, 3 * 24 * 60 * 60, 7 * 24 * 60 * 60})
MIN_SANCTION_DURATION_SECONDS = 60
MAX_SANCTION_DURATION_SECONDS = 10080 * 60
SANCTION_ACTION_LABELS = {
    "rate_limit": "Rate-limit", "mute": "Mute", "temporary_block": "\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0430",
    "permanent_block": "\u041f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u0430\u044f \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0430", "warning": "\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435",
}


def sanction_duration_text(seconds: int) -> str:
    labels = {10 * 60: "10 \u043c\u0438\u043d.", 60 * 60: "1 \u0447.", 6 * 60 * 60: "6 \u0447.", 24 * 60 * 60: "24 \u0447.", 3 * 24 * 60 * 60: "3 \u0434\u043d.", 7 * 24 * 60 * 60: "7 \u0434\u043d."}
    return labels.get(seconds, f"{seconds // 60} \u043c\u0438\u043d.")


def _sanction_duration_seconds(action: str, parameters: dict[str, object]) -> int | None:
    if action == "rate_limit":
        value = parameters.get("rate_limit_seconds", parameters.get("duration_seconds"))
    elif action in {"mute", "temporary_block"}:
        value = parameters.get("duration_seconds")
    else:
        return None
    return value if isinstance(value, int) else None


def _valid_sanction_duration(action: str, seconds: int | None) -> bool:
    if action not in {"rate_limit", "mute", "temporary_block"}:
        return seconds is None
    return isinstance(seconds, int) and MIN_SANCTION_DURATION_SECONDS <= seconds <= MAX_SANCTION_DURATION_SECONDS


def sanction_flow_is_complete(data: dict[str, object]) -> bool:
    if not isinstance(data.get("channel_id"), int) or not isinstance(data.get("target_user_id"), int):
        return False
    if data.get("privacy_mode") not in {"anonymous", "identified"}:
        return False
    action = data.get("sanction_type")
    if action not in SANCTION_ACTIONS:
        return False
    parameters = data.get("sanction_parameters")
    if not isinstance(parameters, dict):
        return False
    duration = _sanction_duration_seconds(str(action), parameters)
    if action in {"rate_limit", "mute", "temporary_block"} and not _valid_sanction_duration(str(action), duration):
        return False
    if action in {"permanent_block", "warning"} and parameters:
        return False
    choice = data.get("reason_choice")
    if choice not in SANCTION_REASON_CHOICES:
        return False
    if choice == "other" and (
        not isinstance(data.get("custom_reason"), str) or not data["custom_reason"].strip()
    ):
        return False
    return type(data.get("show_reason_to_subscriber")) is bool


def sanction_confirmation_values(data: dict[str, object], *, anonymous_tag: str | None = None) -> dict[str, object]:
    if not sanction_flow_is_complete(data):
        raise ValueError("Incomplete sanction flow")
    reason = Database.resolve_sanction_reason(
        str(data["reason_choice"]),
        data.get("custom_reason") if isinstance(data.get("custom_reason"), str) else None,
    )
    action = str(data["sanction_type"])
    duration = _sanction_duration_seconds(action, data["sanction_parameters"])
    target = (
        f"Анонимная подписчица: {anonymous_tag or 'Аноним'}"
        if data["privacy_mode"] == "anonymous"
        else f"Подписчица #{int(data['target_user_id'])}"
    )
    return {
        "target": target,
        "action": SANCTION_ACTION_LABELS[action],
        "parameter": f"\nСрок/интервал: {sanction_duration_text(duration)}" if duration is not None else "",
        "reason": reason,
        "visible": "да" if data["show_reason_to_subscriber"] else "нет",
    }


def sanction_confirmation_text(data: dict[str, object], *, anonymous_tag: str | None = None) -> str:
    return render_default("sanction.flow.confirmation", sanction_confirmation_values(data, anonymous_tag=anonymous_tag))


def rate_limit_notification_text(*, event: str, seconds: int, until: datetime | None, reason: str | None, show_reason: bool) -> str:
    if event not in {"applied", "active"}:
        raise ValueError("Unknown rate limit notification event")
    if not _valid_sanction_duration("rate_limit", seconds):
        raise ValueError("Invalid rate limit")
    if event == "active" and until is None:
        raise ValueError("Active rate limit requires next allowed time")
    key = f"sanction.rate.{event}.{'visible' if show_reason else 'hidden'}"
    values = {
        "duration": sanction_duration_text(seconds),
        "reason": reason or "",
    }
    if until is not None:
        values["expires_at"] = until.strftime("%d.%m.%Y %H:%M UTC")
    return render_default(key, values)


def _sanction_notice_duration(action: str, until: datetime | None) -> str:
    if action == "warning":
        return ""
    if action == "permanent_block":
        return "Доступ ограничен бессрочно."
    if until is not None:
        return f"До: {until.strftime('%d.%m.%Y %H:%M UTC')}."
    return ""


def sanction_notification_text(*, event: str, action: str, until: datetime | None, reason: str | None, show_reason: bool) -> str:
    key = f"sanction.{event}.{'visible' if show_reason else 'hidden'}"
    return render_default(key, {
        "action": SANCTION_ACTION_LABELS[action],
        "duration": _sanction_notice_duration(action, until),
        "reason": reason or "",
    })


async def deliver_sanction_notification(*, bot: Bot, user_id: int, action: str, until: datetime | None, reason: str | None, show_reason: bool, db: Database | None = None, channel_id: int | None = None) -> bool:
    key = f"sanction.applied.{'visible' if show_reason else 'hidden'}"
    values = {
        "action": SANCTION_ACTION_LABELS[action],
        "duration": _sanction_notice_duration(action, until),
        "reason": reason or "",
    }
    text = (
        await render_template(db, channel_id, key, **values)
        if db is not None and channel_id is not None
        else sanction_notification_text(
            event="applied", action=action, until=until, reason=reason, show_reason=show_reason
        )
    )
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except TelegramAPIError:
        logger.warning("Unable to deliver sanction notification to subscriber user_id=%s", user_id)
        return False
    return True


async def deliver_rate_limit_notification(*, bot: Bot, user_id: int, seconds: int, until: datetime | None, reason: str | None, show_reason: bool, db: Database | None = None, channel_id: int | None = None) -> bool:
    if not _valid_sanction_duration("rate_limit", seconds):
        raise ValueError("Invalid rate limit")
    key = f"sanction.rate.applied.{'visible' if show_reason else 'hidden'}"
    values = {
        "duration": sanction_duration_text(seconds),
        "reason": reason or "",
    }
    text = (
        await render_template(db, channel_id, key, **values)
        if db is not None and channel_id is not None
        else rate_limit_notification_text(
            event="applied", seconds=seconds, until=until, reason=reason, show_reason=show_reason
        )
    )
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except TelegramAPIError:
        logger.warning("Unable to deliver rate-limit notification to subscriber user_id=%s", user_id)
        return False
    return True


def sanction_reason_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for choice in SANCTION_REASON_CHOICES:
        label = SANCTION_REASON_LABELS.get(choice, "\u0414\u0440\u0443\u0433\u043e\u0435")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"sanction:reason:{choice}")])
    rows.append([InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="sanction:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sanction_visibility_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u0414\u0430", callback_data="sanction:visibility:yes"), InlineKeyboardButton(text="\u041d\u0435\u0442", callback_data="sanction:visibility:no")],
        [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="sanction:cancel")],
    ])


def sanction_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u041f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c", callback_data="sanction:apply")],
        [InlineKeyboardButton(text="\u041e\u0442\u043c\u0435\u043d\u0430", callback_data="sanction:cancel")],
    ])


def sanction_duration_keyboard(action: str) -> InlineKeyboardMarkup:
    if action not in {"rate_limit", "mute", "temporary_block"}:
        raise ValueError("Unsupported timed sanction")
    choices = sorted(RATE_LIMIT_SECONDS if action == "rate_limit" else DURATION_SECONDS)
    rows = [
        [
            InlineKeyboardButton(
                text=sanction_duration_text(seconds),
                callback_data=f"sanction:param:{action}:{seconds}",
            )
            for seconds in choices[index:index + 3]
        ]
        for index in range(0, len(choices), 3)
    ]
    rows.append([
        InlineKeyboardButton(text="Другой интервал", callback_data=f"sanction:param:{action}:custom"),
        InlineKeyboardButton(text="Отмена", callback_data="sanction:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ADMIN_COMMANDS = {
    "setup",
    "panel",
    "set_period",
    "set_announcement",
    "set_timezone",
    "channels",
    "stats",
    "status",
    "set_topic_template",
    "subscriber",
    "subscriber_history",
    "broadcast",
}



def analytics_message_type(message: Message) -> str:
    if message.photo: return "photo"
    if message.video: return "video"
    if message.voice: return "voice"
    if message.document: return "document"
    if message.audio: return "audio"
    if message.sticker: return "sticker"
    if message.animation: return "animation"
    if message.text: return "text"
    return "other"


def validate_topic_template(template: str, *, privacy_mode: str) -> None:
    if not template or len(template) > 128:
        raise ValueError("Длина шаблона должна быть от 1 до 128 символов")
    allowed = {"anonymous_tag"} if privacy_mode == "anonymous" else {"name", "username", "user_id"}
    try:
        fields = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("Некорректный шаблон") from exc
    for _, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue
        if field_name not in allowed or format_spec or conversion:
            raise ValueError("Можно использовать только разрешённые переменные")


def topic_name(channel, user: User, *, privacy_mode: str, anonymous_tag: str | None = None) -> str:
    template = str(channel["anonymous_topic_template"] if privacy_mode == "anonymous" else channel["identified_topic_template"])
    validate_topic_template(template, privacy_mode=privacy_mode)
    if privacy_mode == "anonymous":
        return template.format(anonymous_tag=anonymous_tag or "Анон")[:128]
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Не указано"
    username = f"@{user.username}" if user.username else "без username"
    return template.format(name=full_name, username=username, user_id=user.id)[:128]



def topic_card_values(user: User, card, *, privacy_mode: str, anonymous_tag: str | None = None) -> tuple[str, dict[str, object]]:
    first_seen = str(card["first_seen_at"]).replace("T", " ")[:16] if card else "неизвестно"
    last_seen = str(card["last_seen_at"]).replace("T", " ")[:16] if card else "неизвестно"
    messages = int(card["message_count"] or 0) if card else 0
    if privacy_mode == "anonymous":
        tag = anonymous_tag or (str(card["anonymous_tag"]) if card and card["anonymous_tag"] else "Анон")
        return "subscriber.card.anonymous", {
            "anonymous_tag": tag,
            "message_count": messages,
            "last_seen": last_seen,
        }
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Не указано"
    username = f"@{user.username}" if user.username else "не указан"
    return "subscriber.card.identified", {
        "name": name,
        "username": username,
        "user_id": user.id,
        "first_seen": first_seen,
        "message_count": messages,
        "last_seen": last_seen,
    }


async def render_topic_card(db: Database, channel_id: int, user: User, card, *, privacy_mode: str, anonymous_tag: str | None = None) -> str:
    key, values = topic_card_values(user, card, privacy_mode=privacy_mode, anonymous_tag=anonymous_tag)
    return await render_template(db, channel_id, key, **values)




STATISTICS_PAGES = ("overview", "messages", "responses", "activity", "top", "admins")
STATISTICS_PERIODS = ("today", "7d", "30d", "all")
STATISTICS_PAGE_LABELS = {
    "overview": "Обзор", "messages": "Сообщения", "responses": "Ответы",
    "activity": "Активность", "top": "Топ подписчиц", "admins": "Администраторы",
}
STATISTICS_PERIOD_LABELS = {
    "today": "Сегодня", "7d": "7 дней", "30d": "30 дней", "all": "Всё время",
}
STATISTICS_UI_LABELS = {"no_data": "—"}


def statistics_duration(value: object) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes or hours:
        parts.append(f"{minutes} мин")
    if not hours and not minutes or seconds:
        parts.append(f"{seconds} сек")
    return " ".join(parts)


def statistics_text(stats: dict[str, object], page: str) -> str:
    """Render only already calculated statistics; never calculate them here."""
    if page not in STATISTICS_PAGES:
        page = "overview"
    period = STATISTICS_PERIOD_LABELS.get(str(stats.get("period")), "Всё время")
    legacy = ""
    if page == "overview":
        body = (
            f"Уникальные подписчицы: <b>{int(stats['unique_subscribers'])}</b>\n"
            f"Активны: 1д — {int(stats['active_subscribers_1d'])}; "
            f"7д — {int(stats['active_subscribers_7d'])}; 30д — {int(stats['active_subscribers_30d'])}\n"
            f"Новые за период: <b>{int(stats['new_subscribers'])}</b>\n"
            f"Сообщения подписчиц: <b>{int(stats['subscriber_messages'])}</b>\n"
            f"Ответы администраторов: <b>{int(stats['admin_replies'])}</b>\n"
            f"Среднее сообщений на подписчицу: {float(stats['average_messages_per_subscriber']):.2f}\n"
            f"Обращения: {int(stats['conversation_count'])}; "
            f"С ответом: {int(stats['answered_conversation_count'])} "
            f"({float(stats['answered_conversation_share']):.1f}%)"
        )
    elif page == "messages":
        media = stats["media"]
        body = (
            f"Текст: {int(media['text'])}\nФото: {int(media['photo'])}\n"
            f"Видео: {int(media['video'])}\nДокументы: {int(media['document'])}\n"
            f"Голосовые: {int(media['voice'])}\nАудио: {int(media['audio'])}\n"
            f"Стикеры: {int(media['sticker'])}\nДругое: {int(media['other'])}\n\n"
            f"Альбомы: <b>{int(stats['album_count'])}</b>\n"
            f"Медиаэлементов: <b>{int(stats['media_items_count'])}</b>"
        )
    elif page == "responses":
        body = (
            f"Обращения: <b>{int(stats['conversation_count'])}</b>\n"
            f"С ответом: <b>{int(stats['answered_conversation_count'])}</b>\n"
            f"Доля отвеченных: <b>{float(stats['answered_conversation_share']):.1f}%</b>\n\n"
            f"Среднее время первого ответа: <b>{statistics_duration(stats['average_first_response_seconds'])}</b>\n"
            f"Медиана времени первого ответа: <b>{statistics_duration(stats['median_first_response_seconds'])}</b>"
        )
    elif page == "activity":
        hours = stats["messages_by_hour"]
        weekdays = stats["messages_by_weekday"]
        weekday_names = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        active_hours = [(hour, int(count)) for hour, count in hours.items() if int(count)]
        top_hours = sorted(active_hours, key=lambda item: (-item[1], item[0]))[:5]
        hours_text = "\n".join(f"{hour:02d}:00 — {count}" for hour, count in top_hours) or STATISTICS_UI_LABELS["no_data"]
        weekdays_text = " · ".join(f"{weekday_names[int(day)]} {int(count)}" for day, count in weekdays.items())
        active_hour = stats['most_active_hour']
        active_day = stats['most_active_weekday']
        body = (
            f"Самый активный час: <b>{f'{int(active_hour):02d}:00' if active_hour is not None else '—'}</b>\n"
            f"Самый активный день: <b>{weekday_names[int(active_day)] if active_day is not None else '—'}</b>\n\n"
            f"Топ часов:\n{hours_text}\n\nПо дням недели:\n{weekdays_text}"
        )
    else:
        top = stats["top_subscribers"]
        if not top:
            body = STATISTICS_UI_LABELS["no_data"]
        else:
            lines = []
            for position, row in enumerate(top, start=1):
                # The data API deliberately omits identity fields for anonymous rows.
                lines.append(f"{position}. {html.escape(str(row['display_name']))} — <b>{int(row['message_count'])}</b>")
            body = "\n".join(lines)
    return body



def admin_statistics_text(stats: dict[str, object], *, detail: int | None = None) -> str:
    legacy = ""
    admins = stats["admins"]
    if detail is not None and 0 <= detail < len(admins):
        row = admins[detail]
        return (f"<b>Администратор — {html.escape(str(row['display_name']))}</b>\n"
                f"Ответы: <b>{int(row['reply_count'])}</b>\nДиалоги: {int(row['unique_conversations_replied'])}\n"
                f"Обработано обращений: <b>{int(row['handled_conversations'])}</b>\n"
                f"Среднее: {statistics_duration(row['average_first_response_seconds'])}\nМедиана: {statistics_duration(row['median_first_response_seconds'])}\n\n"
                f"Действия модерации: {int(row['moderation_actions'])}\nОграничения: {int(row['restrictions'])}; предупреждения: {int(row['warnings'])}; спам: {int(row['spam_marks'])}{legacy}")
    handled_conversations = int(
        stats.get("handled_conversation_count", stats.get("answered_conversation_count", 0)) or 0
    )
    tracked_conversations = int(
        stats.get("tracked_conversation_count", stats.get("conversation_count", handled_conversations)) or 0
    )
    unanswered_conversations = int(
        stats.get("unanswered_conversation_count", max(0, tracked_conversations - handled_conversations)) or 0
    )
    lines = [
        f"Активных: {int(stats.get('active_admin_count', 0) or 0)}; ответов: {int(stats.get('admin_replies', 0) or 0)}",
        f"Обработано обращений: <b>{handled_conversations}</b>; без ответа: <b>{unanswered_conversations}</b>",
        f"Средний ответ: {statistics_duration(stats.get('team_average_first_response_seconds'))}; медиана: {statistics_duration(stats.get('team_median_first_response_seconds'))}",
        f"Лидер по ответам: {html.escape(str(stats.get('top_reply_admin') or '—'))}; по первым ответам: {html.escape(str(stats.get('top_first_response_admin') or '—'))}",
    ]
    if admins:
        lines.append("\n" + "\n".join(f"{i}. {html.escape(str(row['display_name']))} — <b>{int(row['reply_count'])}</b>" for i,row in enumerate(admins,1)))
    else: lines.append(STATISTICS_UI_LABELS["no_data"])
    return "\n".join(lines)+legacy

async def render_statistics_page(*, db: Database, channel_id: int, stats: dict[str, object], page: str) -> str:
    """Apply the effective channel template around already calculated statistics."""
    key = "statistics.admins" if page == "admins" else f"statistics.page.{page if page in STATISTICS_PAGES else 'overview'}"
    body = admin_statistics_text(stats) if page == "admins" else statistics_text(stats, page)
    if (page == "admins" and not stats.get("admins")) or (page != "admins" and not int(stats.get("subscriber_messages", 0))):
        body = await render_template(db, channel_id, "statistics.no_data")
    # Body contains only structural metrics. TemplateRenderer escapes it, so
    # remove presentation markup before passing it as an untrusted placeholder.
    body = html.unescape(body).replace("<b>", "").replace("</b>", "")
    legacy_warning = ""
    if not bool(stats.get("conversation_metrics_complete")):
        legacy_warning = "\n\n" + await render_template(db, channel_id, "statistics.legacy_warning")
    return await render_template(db, channel_id, key, body=body, page_title=STATISTICS_PAGE_LABELS.get(page, STATISTICS_PAGE_LABELS['overview']), period=STATISTICS_PERIOD_LABELS.get(str(stats.get('period')), '—'), legacy_warning=legacy_warning)


def statistics_keyboard(*, source: str, page: str = "overview", period: str = "all") -> InlineKeyboardMarkup:
    if source not in {"stats", "panel"}:
        raise ValueError("Unknown statistics source")
    if page not in STATISTICS_PAGES:
        page = "overview"
    if period not in STATISTICS_PERIODS:
        period = "all"
    prefix = "stats" if source == "stats" else "panel:stats"
    callback = lambda next_page, next_period: f"{prefix}:{next_page}:{next_period}"
    page_rows = [
        [InlineKeyboardButton(text=STATISTICS_PAGE_LABELS[key], callback_data=callback(key, period)) for key in STATISTICS_PAGES[:3]],
        [InlineKeyboardButton(text=STATISTICS_PAGE_LABELS[key], callback_data=callback(key, period)) for key in STATISTICS_PAGES[3:5]],
        [InlineKeyboardButton(text=STATISTICS_PAGE_LABELS["admins"], callback_data=callback("admins", period))],
    ]
    period_row = [InlineKeyboardButton(text=STATISTICS_PERIOD_LABELS[key], callback_data=callback(page, key)) for key in STATISTICS_PERIODS]
    back = "stats:back" if source == "stats" else "panel:home"
    export_row = [InlineKeyboardButton(text="Экспорт", callback_data=f"panel:export:{period}")] if source == "panel" else []
    return InlineKeyboardMarkup(inline_keyboard=page_rows + [period_row] + ([export_row] if export_row else []) + [[InlineKeyboardButton(text="Назад", callback_data=back)]])


async def _statistics_callback_channel(callback: CallbackQuery, authorizer: ChannelAuthorizer, *, source: str):
    if callback.message is None or callback.from_user is None:
        return None
    if source == "panel":
        channel = await _panel_callback_channel(callback, authorizer)
    else:
        if callback.message.chat.type != ChatType.SUPERGROUP:
            channel = None
        else:
            channel = await authorizer.db.get_channel_by_group(callback.message.chat.id)
            if channel is not None:
                decision = await authorizer.require(actor_id=callback.from_user.id, channel_id=int(channel["channel_id"]), action=ChannelAction.STATISTICS, context_group_id=callback.message.chat.id, require_current_telegram_admin=True)
                channel = decision.channel if decision.allowed else None
    if channel is None or not bool(channel["enabled"]):
        await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
        return None
    return channel


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обзор", callback_data="panel:home"), InlineKeyboardButton(text="Статистика", callback_data="panel:stats")],
        [InlineKeyboardButton(text="Автоочистка", callback_data="panel:cleanup"), InlineKeyboardButton(text="Уведомления", callback_data="panel:notices")],
        [InlineKeyboardButton(text="Поиск", callback_data="panel:search"), InlineKeyboardButton(text="Анонимность", callback_data="panel:anonymous")],
        [InlineKeyboardButton(text="Тексты", callback_data="panel:texts"), InlineKeyboardButton(text="Ручная очистка", callback_data="panel:manual_cleanup_preview")],
        [InlineKeyboardButton(text="Оформление бота", callback_data="panel:prestart"), InlineKeyboardButton(text="Реакции", callback_data="panel:reactions")],
        [InlineKeyboardButton(text="Обновить", callback_data="panel:refresh")],
    ])


def cleanup_keyboard(channel) -> InlineKeyboardMarkup:
    enabled = bool(channel["auto_cleanup_enabled"])
    toggle = InlineKeyboardButton(text="Отключить автоочистку" if enabled else "Включить автоочистку", callback_data="panel:cleanup:disable" if enabled else "panel:cleanup:enable_menu")
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text="По созданию", callback_data="panel:cleanup:basis:created_at"), InlineKeyboardButton(text="По активности", callback_data="panel:cleanup:basis:last_activity_at")],
        [InlineKeyboardButton(text="Все темы", callback_data="panel:cleanup:scope:all"), InlineKeyboardButton(text="Только завершённые", callback_data="panel:cleanup:scope:answered_closed")],
        [InlineKeyboardButton(text="Удалять", callback_data="panel:cleanup:action:delete"), InlineKeyboardButton(text="Закрывать", callback_data="panel:cleanup:action:close")],
        [InlineKeyboardButton(text="Закрыть и удалить", callback_data="panel:cleanup:action:close_then_delete")],
        [InlineKeyboardButton(text="Назад", callback_data="panel:home")],
    ])


def admin_channel_selection_keyboard(channels) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(channel["group_title"])[:60], callback_data=f"panel:select:{int(channel['channel_id'])}")
    ] for channel in channels])


def cleanup_enable_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="7 дней", callback_data="panel:cleanup:enable:7"),
        InlineKeyboardButton(text="30 дней", callback_data="panel:cleanup:enable:30"),
        InlineKeyboardButton(text="90 дней", callback_data="panel:cleanup:enable:90"),
    ], [InlineKeyboardButton(text="Назад", callback_data="panel:cleanup")]])


def anonymous_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить префикс", callback_data="panel:anonymous:edit")],
        [InlineKeyboardButton(text="Назад", callback_data="panel:home")],
    ])


def reaction_settings_keyboard(settings: dict[str, object]) -> InlineKeyboardMarkup:
    has_topic = settings.get("service_topic_id") is not None
    rows = [
        [InlineKeyboardButton(text="Режим 1", callback_data="panel:reactions:mode1"),
         InlineKeyboardButton(text="Режим 2", callback_data="panel:reactions:mode2")],
    ]
    if has_topic:
        rows.append([InlineKeyboardButton(text="Переименовать ветку", callback_data="panel:reactions:rename")])
        rows.append([InlineKeyboardButton(text="Пересоздать ветку", callback_data="panel:reactions:recreate")])
    else:
        rows.append([InlineKeyboardButton(text="Создать служебную ветку", callback_data="panel:reactions:create")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="panel:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def reaction_settings_text(db: Database, channel_id: int) -> str:
    settings = await db.get_channel_reaction_settings(channel_id)
    mode = "Режим 2" if settings["mode"] == "service" else "Режим 1"
    topic = str(settings["service_topic_name"] or "не создана")
    repair = "требуется восстановление" if settings["requires_repair"] else "готово"
    return await render_template(db, channel_id, "reaction.settings_overview", mode=mode, topic=topic, repair=repair)


def prestart_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить текст", callback_data="panel:prestart:text")],
        [InlineKeyboardButton(text="Заменить медиа", callback_data="panel:prestart:media")],
        [InlineKeyboardButton(text="Подготовить медиа к применению", callback_data="panel:prestart:media_apply")],
        [InlineKeyboardButton(text="Удалить медиа", callback_data="panel:prestart:media_remove")],
        [InlineKeyboardButton(text="Предпросмотр", callback_data="panel:prestart:preview")],
        [InlineKeyboardButton(text="Вернуть стандартные", callback_data="panel:prestart:reset")],
        [InlineKeyboardButton(text="Назад", callback_data="panel:home")],
    ])


async def effective_prestart_description(bot: Bot, db: Database) -> str:
    try:
        current = await bot.get_my_description()
        value = getattr(current, "description", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    except TelegramAPIError:
        logger.warning("Unable to read current bot description; using persisted/default value")
    row = await db.get_bot_prestart_card()
    if row is not None and row["description_override"]:
        return str(row["description_override"])
    return DEFAULT_PRESTART_DESCRIPTION


async def send_prestart_preview(*, message: Message, bot: Bot, db: Database, draft_media: tuple[str, str] | None = None, draft_text: str | None = None) -> None:
    description = draft_text or await effective_prestart_description(bot, db)
    row = await db.get_bot_prestart_card()
    media_type = draft_media[0] if draft_media else (str(row["media_type"]) if row is not None and row["media_type"] else None)
    media_file_id = draft_media[1] if draft_media else (str(row["media_file_id"]) if row is not None and row["media_file_id"] else None)
    caption = "<b>Предпросмотр карточки до Start</b>\n\n" + html.escape(description)
    if media_type and media_file_id:
        try:
            if media_type == "photo":
                await message.answer_photo(photo=media_file_id, caption=caption)
            elif media_type == "video":
                await message.answer_video(video=media_file_id, caption=caption)
            else:
                await message.answer_animation(animation=media_file_id, caption=caption)
            return
        except TelegramAPIError:
            logger.warning("Unable to render stored pre-start media preview")
    await message.answer(caption)


def channel_selection_keyboard(channels) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(channel["group_title"])[:60], callback_data=f"channel:select:{int(channel['channel_id'])}")
    ] for channel in channels])


async def trusted_active_channel_for_user(db: Database, user_id: int):
    """Return an enabled active channel only after membership is rechecked."""
    channel = await db.get_active_channel_for_user(user_id)
    if channel is None:
        return None
    channel_id = int(channel["channel_id"])
    available = await db.list_enabled_channels_for_user(user_id)
    return channel if any(int(item["channel_id"]) == channel_id for item in available) else None


async def channel_selection_prompt(db: Database, message: Message, channels, *, current_channel=None) -> None:
    if current_channel is None:
        text = render_default("channel.choose", {})
    else:
        text = await render_template(
            db,
            int(current_channel["channel_id"]),
            "channel.choose_current",
            channel_name=str(current_channel["group_title"]),
        )
    await message.answer(text, reply_markup=channel_selection_keyboard(channels))

def is_missing_topic_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "message thread not found",
        "message thread is not found",
        "forum topic not found",
    )
    return any(marker in text for marker in markers)


def is_closed_topic_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = ("topic_closed", "topic closed", "message thread is closed")
    return any(marker in text for marker in markers)


def is_missing_or_closed_topic_error(exc: Exception) -> bool:
    return is_missing_topic_error(exc) or is_closed_topic_error(exc)


def message_is_admin_command(message: Message) -> bool:
    """Keep command-like messages inside the administrative surface.

    Even an unknown slash command must never fall through to the generic admin
    reply handler and become a subscriber message.
    """
    text = (message.text or "").lstrip()
    if not text.startswith("/"):
        return False
    token = text[1:].split(maxsplit=1)[0]
    if not token:
        return False
    command = token.split("@", maxsplit=1)[0]
    return bool(command)


def is_general_forum_message(message: Message) -> bool:
    """Return True only for the General context of a forum supergroup.

    Bot API command scopes cannot target forum topics. Incoming messages from
    ordinary forum topics have ``is_topic_message=True``; General is topic-less
    (even though replies there can still carry a generic ``message_thread_id``).
    The thread-id fallback keeps compatibility with lightweight test doubles.
    """
    if message.chat.type != ChatType.SUPERGROUP:
        return False
    if hasattr(message, "is_topic_message"):
        return not bool(getattr(message, "is_topic_message", False))
    return message.message_thread_id in {None, 1}


def broadcast_message_is_copyable(message: Message) -> bool:
    if any((
        message.forum_topic_created, message.forum_topic_closed, message.forum_topic_reopened,
        message.forum_topic_edited, message.general_forum_topic_hidden,
        message.general_forum_topic_unhidden,
    )):
        return False
    return any(getattr(message, name, None) is not None for name in (
        "text", "photo", "video", "document", "audio", "voice", "animation",
        "sticker", "poll", "contact", "location", "venue", "dice", "video_note",
    ))


def broadcast_preview_keyboard(broadcast_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить", callback_data=f"broadcast:send:{broadcast_id}")],
        [InlineKeyboardButton(text="Редактировать", callback_data=f"broadcast:edit:{broadcast_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"broadcast:cancel:{broadcast_id}")],
    ])


def broadcast_resume_keyboard(broadcast_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Продолжить рассылку", callback_data=f"broadcast:resume:{broadcast_id}")
    ]])


class AlbumBuffer:
    """
    Telegram присылает элементы медиагруппы отдельными updates.
    Небольшая задержка позволяет собрать их и потом вызвать copyMessages,
    сохранив исходную группировку альбома.
    """

    def __init__(
        self,
        *,
        delay: float,
        callback: Callable[[list["BufferedMessage"]], Awaitable[None]],
    ) -> None:
        self.delay = delay
        self.callback = callback
        self._groups: dict[Hashable, list[BufferedMessage]] = {}
        self._tasks: dict[Hashable, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        *,
        key: Hashable | None,
        item: "BufferedMessage",
    ) -> None:
        if key is None:
            await self.callback([item])
            return

        async with self._lock:
            self._groups.setdefault(key, []).append(item)
            if key not in self._tasks:
                self._tasks[key] = asyncio.create_task(
                    self._flush_later(key)
                )

    async def _flush_later(self, key: Hashable) -> None:
        try:
            await asyncio.sleep(self.delay)

            async with self._lock:
                items = self._groups.pop(key, [])
                self._tasks.pop(key, None)

            if items:
                items.sort(key=lambda item: item.message.message_id)
                await self.callback(items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработки медиагруппы %r", key)

    async def close(self, *_) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(slots=True)
class BroadcastAlbumItem:
    message: Message
    state: FSMContext
    channel_id: int
    group_id: int
    owner_id: int


class BroadcastAlbumCollector:
    """Collect Telegram media-group updates into one broadcast publication."""

    def __init__(self, *, delay: float, callback: Callable[[list[BroadcastAlbumItem]], Awaitable[None]]) -> None:
        self.delay = delay
        self.callback = callback
        self._groups: dict[tuple[int, int, str], list[BroadcastAlbumItem]] = {}
        self._tasks: dict[tuple[int, int, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def push(self, item: BroadcastAlbumItem) -> None:
        media_group_id = item.message.media_group_id
        if not media_group_id:
            raise ValueError("Broadcast album item has no media_group_id")
        key = (item.group_id, item.owner_id, str(media_group_id))
        async with self._lock:
            group = self._groups.setdefault(key, [])
            if all(existing.message.message_id != item.message.message_id for existing in group):
                group.append(item)
            if key not in self._tasks:
                self._tasks[key] = asyncio.create_task(self._flush_later(key))

    async def _flush_later(self, key: tuple[int, int, str]) -> None:
        try:
            await asyncio.sleep(self.delay)
            async with self._lock:
                items = self._groups.pop(key, [])
                self._tasks.pop(key, None)
            if items:
                items.sort(key=lambda item: item.message.message_id)
                await self.callback(items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработки альбома рассылки %r", key)

    async def close(self, *_) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(slots=True)
class BufferedMessage:
    message: Message
    channel_id: int
    group_id: int
    user_id: int
    topic_id: int | None = None
    privacy_mode: str = "identified"


class FeedbackRuntime:
    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
        media_group_delay: float,
    ) -> None:
        self.bot = bot
        self.db = db

        self._topic_locks: defaultdict[
            tuple[int, int], asyncio.Lock
        ] = defaultdict(asyncio.Lock)
        self._delivery_locks: defaultdict[
            tuple[int, int], asyncio.Lock
        ] = defaultdict(asyncio.Lock)

        self.user_albums = AlbumBuffer(
            delay=media_group_delay,
            callback=self._flush_user_messages,
        )
        self.admin_albums = AlbumBuffer(
            delay=media_group_delay,
            callback=self._flush_admin_messages,
        )

    async def close(self) -> None:
        await self.user_albums.close()
        await self.admin_albums.close()

    async def remember_user(self, user: User) -> None:
        await self.db.upsert_user(
            user_id=user.id,
            first_name=user.first_name or "Пользователь",
            last_name=user.last_name,
            username=user.username,
        )

    async def get_or_create_topic(
        self,
        *,
        channel_id: int,
        user: User,
        privacy_mode: str,
        anonymous_tag: str | None = None,
    ) -> tuple[int, int]:
        channel = await self.db.get_channel_by_id(channel_id)
        if channel is None:
            raise RuntimeError("Channel больше не существует")

        existing = await self.db.get_topic_for_user(
            channel_id=channel_id,
            user_id=user.id,
            privacy_mode=privacy_mode,
        )
        if existing is not None:
            return int(existing["group_id"]), int(existing["topic_id"])

        lock_key = (channel_id, user.id)

        async with self._topic_locks[lock_key]:
            existing = await self.db.get_topic_for_user(
                channel_id=channel_id,
                user_id=user.id,
                privacy_mode=privacy_mode,
            )
            if existing is not None:
                return int(existing["group_id"]), int(existing["topic_id"])

            group_id = int(channel["group_id"])

            topic = await self.bot.create_forum_topic(
                chat_id=group_id,
                name=topic_name(channel, user, privacy_mode=privacy_mode, anonymous_tag=anonymous_tag),
            )

            await self.db.create_topic_mapping(
                channel_id=channel_id,
                user_id=user.id,
                privacy_mode=privacy_mode,
                group_id=group_id,
                topic_id=topic.message_thread_id,
            )

            card = await self.db.get_subscriber_card_data(channel_id=channel_id, user_id=user.id, privacy_mode=privacy_mode)
            await self.bot.send_message(
                chat_id=group_id,
                message_thread_id=topic.message_thread_id,
                text=await render_topic_card(self.db, channel_id, user, card, privacy_mode=privacy_mode, anonymous_tag=anonymous_tag),
                disable_web_page_preview=True,
            )

            return group_id, topic.message_thread_id

    async def accept_user_message(
        self,
        *,
        message: Message,
        channel_id: int,
        group_id: int,
        privacy_mode: str,
    ) -> None:
        if not message.from_user:
            return

        await self.remember_user(message.from_user)
        await self.db.touch_subscriber(
            channel_id=channel_id,
            user_id=message.from_user.id,
        )

        key = None
        if message.media_group_id:
            key = (
                "user",
                channel_id,
                message.chat.id,
                message.media_group_id,
            )

        await self.user_albums.push(
            key=key,
            item=BufferedMessage(
                message=message,
                channel_id=channel_id,
                group_id=group_id,
                user_id=message.from_user.id,
                privacy_mode=privacy_mode,
            ),
        )

    async def _copy_user_batch_to_topic(
        self,
        *,
        items: list[BufferedMessage],
        group_id: int,
        topic_id: int,
    ) -> None:
        ordered = sorted(items, key=lambda item: item.message.message_id)
        first = ordered[0].message
        message_ids = [item.message.message_id for item in ordered]

        if len(message_ids) == 1:
            copied = await self.bot.copy_message(
                chat_id=group_id,
                message_thread_id=topic_id,
                from_chat_id=first.chat.id,
                message_id=message_ids[0],
            )
            copied_ids = [int(copied.message_id)]
        else:
            copied = await self.bot.copy_messages(
                chat_id=group_id,
                message_thread_id=topic_id,
                from_chat_id=first.chat.id,
                message_ids=message_ids,
            )
            copied_ids = [int(item.message_id) for item in copied]

        if len(copied_ids) != len(ordered):
            raise RuntimeError("Telegram returned an unexpected copied-message count")
        for item, forum_message_id in zip(ordered, copied_ids):
            await self.db.record_reaction_source(
                channel_id=item.channel_id,
                group_id=group_id,
                forum_message_id=forum_message_id,
                user_id=item.user_id,
                privacy_mode=item.privacy_mode,
                private_chat_id=item.message.chat.id,
                private_message_id=item.message.message_id,
                topic_id=topic_id,
            )

    async def _flush_user_messages(
        self,
        items: list[BufferedMessage],
    ) -> None:
        first = items[0]
        lock_key = (first.channel_id, first.user_id)
        async with self._delivery_locks[lock_key]:
            await self._flush_user_messages_locked(items)

    async def _flush_user_messages_locked(
        self,
        items: list[BufferedMessage],
    ) -> None:
        first = items[0]
        restriction = await self.db.active_subscriber_restriction_details(
            channel_id=first.channel_id, user_id=first.user_id
        )
        if restriction is not None:
            kind, until, reason, show_reason = restriction
            if kind == "rate_limited" and until is not None:
                state = await self.db.get_subscriber_moderation(
                    channel_id=first.channel_id, user_id=first.user_id
                )
                seconds = int(state["rate_limit_seconds"]) if state and state["rate_limit_seconds"] else 0
                if _valid_sanction_duration("rate_limit", seconds):
                    await self.bot.send_message(
                        chat_id=first.user_id,
                        text=await render_template(
                            self.db, first.channel_id,
                            f"sanction.rate.active.{'visible' if show_reason else 'hidden'}",
                            expires_at=until.strftime("%d.%m.%Y %H:%M UTC"),
                            reason=str(reason) if reason else "",
                        ),
                    )
            else:
                action_by_kind={"permanently_blocked":"permanent_block","blocked":"temporary_block","muted":"mute"}
                action=action_by_kind[kind]
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=await render_template(
                        self.db, first.channel_id,
                        f"sanction.active.{'visible' if show_reason else 'hidden'}",
                        action=SANCTION_ACTION_LABELS[action],
                        duration=_sanction_notice_duration(action, until),
                        reason=str(reason) if reason else "",
                    ),
                )
            return
        user = first.message.from_user
        if user is None:
            return

        try:
            group_id, topic_id = await self.get_or_create_topic(
                channel_id=first.channel_id,
                user=user,
                privacy_mode=first.privacy_mode,
                anonymous_tag=(await self.db.ensure_anonymous_tag(channel_id=first.channel_id, user_id=first.user_id)) if first.privacy_mode == "anonymous" else None,
            )

            try:
                await self._copy_user_batch_to_topic(
                    items=items,
                    group_id=group_id,
                    topic_id=topic_id,
                )
            except TelegramBadRequest as exc:
                if not is_missing_or_closed_topic_error(exc):
                    raise

                # Тему могли вручную удалить/закрыть в Telegram.
                # Сбрасываем mapping и автоматически создаём новую.
                await self.db.delete_topic_mapping(
                    channel_id=first.channel_id,
                    user_id=first.user_id,
                    privacy_mode=first.privacy_mode,
                )

                group_id, topic_id = await self.get_or_create_topic(
                    channel_id=first.channel_id,
                    user=user,
                    privacy_mode=first.privacy_mode,
                    anonymous_tag=(await self.db.ensure_anonymous_tag(channel_id=first.channel_id, user_id=first.user_id)) if first.privacy_mode == "anonymous" else None,
                )

                await self._copy_user_batch_to_topic(
                    items=items,
                    group_id=group_id,
                    topic_id=topic_id,
                )

            for item in items:
                await self.db.record_message_event(channel_id=first.channel_id, user_id=first.user_id, privacy_mode=first.privacy_mode, direction="subscriber_to_admin", message_type=analytics_message_type(item.message), occurred_at=item.message.date, source_chat_id=item.message.chat.id, source_message_id=item.message.message_id, media_group_id=item.message.media_group_id, conversation_id=topic_id)
            await self.db.touch_topic(
                channel_id=first.channel_id,
                user_id=first.user_id,
                privacy_mode=first.privacy_mode,
            )

        except TelegramForbiddenError:
            logger.exception(
                "Бот потерял доступ к группе channel=%s",
                first.channel_id,
            )
            try:
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=await render_template(
                        self.db, first.channel_id, "message.channel_unavailable"
                    ),
                )
            except Exception:
                pass

        except TelegramBadRequest as exc:
            logger.exception(
                "Не удалось передать сообщение channel=%s user=%s: %s",
                first.channel_id,
                first.user_id,
                exc,
            )
            try:
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=await render_template(
                        self.db, first.channel_id, "message.unsupported_type"
                    ),
                )
            except Exception:
                pass

    async def accept_admin_message(
        self,
        *,
        message: Message,
        channel_id: int,
        user_id: int,
        group_id: int,
        topic_id: int,
        privacy_mode: str,
    ) -> None:
        key = None
        if message.media_group_id:
            key = (
                "admin",
                group_id,
                topic_id,
                message.media_group_id,
            )

        await self.admin_albums.push(
            key=key,
            item=BufferedMessage(
                message=message,
                channel_id=channel_id,
                group_id=group_id,
                user_id=user_id,
                topic_id=topic_id,
                privacy_mode=privacy_mode,
            ),
        )

    async def _flush_admin_messages(
        self,
        items: list[BufferedMessage],
    ) -> None:
        first = items[0]
        message_ids = sorted(
            item.message.message_id for item in items
        )

        try:
            if len(message_ids) == 1:
                await self.bot.copy_message(
                    chat_id=first.user_id,
                    from_chat_id=first.group_id,
                    message_id=message_ids[0],
                )
            else:
                await self.bot.copy_messages(
                    chat_id=first.user_id,
                    from_chat_id=first.group_id,
                    message_ids=message_ids,
                )

            for item in items:
                await self.db.record_message_event(channel_id=first.channel_id, user_id=first.user_id, privacy_mode=first.privacy_mode, direction="admin_to_subscriber", message_type=analytics_message_type(item.message), occurred_at=item.message.date, source_chat_id=item.message.chat.id, source_message_id=item.message.message_id, admin_id=item.message.from_user.id if item.message.from_user else None, media_group_id=item.message.media_group_id, conversation_id=first.topic_id)
            await self.db.set_user_blocked(first.user_id, False)
            await self.db.mark_topic_answered(channel_id=first.channel_id, user_id=first.user_id, privacy_mode=first.privacy_mode)
            await self.db.touch_topic(channel_id=first.channel_id, user_id=first.user_id, privacy_mode=first.privacy_mode)

        except TelegramForbiddenError:
            await self.db.set_user_blocked(first.user_id, True)

            try:
                await self.bot.send_message(
                    chat_id=first.group_id,
                    message_thread_id=first.topic_id,
                    text=await render_template(
                        self.db, first.channel_id, "reply.user_unavailable"
                    ),
                )
            except Exception:
                pass

        except TelegramBadRequest as exc:
            logger.exception(
                "Ошибка отправки ответа user=%s: %s",
                first.user_id,
                exc,
            )

            try:
                await self.bot.send_message(
                    chat_id=first.group_id,
                    message_thread_id=first.topic_id,
                    text=await render_template(
                        self.db, first.channel_id, "reply.delivery_failed"
                    ),
                )
            except Exception:
                pass


class TopicCleaner:
    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
    ) -> None:
        self.bot = bot
        self.db = db
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def cleanup_rows(
        self,
        *,
        channel_id: int,
        rows,
        action: str = "delete",
    ) -> dict[str, int]:
        result = {
            "deleted": 0,
            "closed": 0,
            "stale": 0,
            "failed": 0,
        }

        async with self._locks[channel_id]:
            for row in rows:
                status = await self._remove_topic(row, action=action)
                result[status] += 1
                await asyncio.sleep(0.05)

        return result

    async def _remove_topic(self, row, *, action: str = "delete") -> str:
        channel_id = int(row["channel_id"])
        if action == "close":
            try:
                await self.bot.close_forum_topic(chat_id=int(row["group_id"]), message_thread_id=int(row["topic_id"]))
                await self.db.mark_topic_auto_closed(channel_id=channel_id, user_id=int(row["user_id"]), privacy_mode=str(row["privacy_mode"]))
                return "closed"
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                if is_missing_topic_error(exc):
                    await self.db.delete_topic_mapping(channel_id=channel_id, user_id=int(row["user_id"]), privacy_mode=str(row["privacy_mode"]))
                    return "stale"
                if is_closed_topic_error(exc):
                    # The topic still exists; preserve its mapping and make the
                    # database state match Telegram instead of treating it as deleted.
                    await self.db.mark_topic_auto_closed(channel_id=channel_id, user_id=int(row["user_id"]), privacy_mode=str(row["privacy_mode"]))
                    return "closed"
                logger.error("Unable to close topic channel=%s topic=%s: %s", channel_id, row["topic_id"], exc)
                return "failed"
        if action != "delete":
            raise ValueError("Unsupported cleanup action")

        user_id = int(row["user_id"])
        group_id = int(row["group_id"])
        topic_id = int(row["topic_id"])

        try:
            await self.bot.delete_forum_topic(
                chat_id=group_id,
                message_thread_id=topic_id,
            )
            await self.db.delete_topic_mapping(
                channel_id=channel_id,
                user_id=user_id,
                privacy_mode=str(row["privacy_mode"]),
            )
            return "deleted"

        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "deleteForumTopic не сработал group=%s topic=%s: %s",
                group_id,
                topic_id,
                exc,
            )
            if is_missing_topic_error(exc):
                await self.db.delete_topic_mapping(
                    channel_id=channel_id,
                    user_id=user_id,
                    privacy_mode=str(row["privacy_mode"]),
                )
                return "stale"
            if not is_closed_topic_error(exc):
                return "failed"

        # A closed topic is not a deleted topic.  Reopen it before retrying the
        # destructive delete; never drop the mapping merely because Telegram
        # reported that the topic was already closed.
        try:
            await self.bot.reopen_forum_topic(
                chat_id=group_id,
                message_thread_id=topic_id,
            )
            await self.bot.delete_forum_topic(
                chat_id=group_id,
                message_thread_id=topic_id,
            )
            await self.db.delete_topic_mapping(
                channel_id=channel_id,
                user_id=user_id,
                privacy_mode=str(row["privacy_mode"]),
            )
            return "deleted"
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if is_missing_topic_error(exc):
                await self.db.delete_topic_mapping(
                    channel_id=channel_id,
                    user_id=user_id,
                    privacy_mode=str(row["privacy_mode"]),
                )
                return "stale"
            logger.error(
                "Не удалось повторно открыть и удалить group=%s topic=%s: %s",
                group_id,
                topic_id,
                exc,
            )
            return "failed"

    async def cleanup_by_policy(self, *, channel, cutoff: datetime, now: datetime) -> dict[str, int]:
        close_rows, delete_rows = await self.db.topics_due_for_auto_cleanup(channel=channel, cutoff=cutoff, now=now)
        result = await self.cleanup_rows(channel_id=int(channel["channel_id"]), rows=close_rows, action="close")
        deleted = await self.cleanup_rows(channel_id=int(channel["channel_id"]), rows=delete_rows, action="delete")
        for key, value in deleted.items():
            result[key] += value
        return result

    async def cleanup_created_before(
        self,
        *,
        channel_id: int,
        cutoff: datetime,
    ) -> dict[str, int]:
        rows = await self.db.topics_created_before(
            channel_id=channel_id,
            cutoff=cutoff,
        )
        return await self.cleanup_rows(
            channel_id=channel_id,
            rows=rows,
        )


class AdminGuard:
    """Compatibility adapter for non-channel administrative messages.

    Channel-scoped sensitive actions use ChannelAuthorizer below.  This helper
    deliberately performs a live membership check and never caches a decision.
    """
    def __init__(self, *, bot: Bot) -> None:
        self.bot = bot

    async def is_group_admin(self, message: Message, group_id: int) -> bool:
        if message.sender_chat and message.sender_chat.id == group_id:
            return True
        if not message.from_user:
            return False
        try:
            return await telegram_group_admin_resolver(self.bot, group_id, message.from_user.id)
        except Exception:
            logger.exception("Unable to validate group admin group=%s user=%s", group_id, message.from_user.id)
            return False


async def telegram_group_admin_resolver(bot: Bot, group_id: int, user_id: int) -> bool:
    """Live Telegram membership adapter used by ChannelAuthorizer."""
    member = await bot.get_chat_member(group_id, user_id)
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}

def _manual_cutoff(timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    now_local = datetime.now(tz)

    # Формулировка ТЗ соблюдается буквально:
    # "созданные ДО 00:00 вчерашнего дня".
    # То есть весь вчерашний и сегодняшний день сохраняются.
    yesterday = now_local.date() - timedelta(days=1)

    cutoff_local = datetime.combine(
        yesterday,
        time.min,
        tzinfo=tz,
    )
    return cutoff_local.astimezone(timezone.utc)


async def _owner_channel_in_group(
    *, message: Message, authorizer: ChannelAuthorizer,
) -> tuple[object | None, str | None]:
    if message.chat.type != ChatType.SUPERGROUP:
        return None, "Команда доступна только в привязанной супергруппе."
    if not is_general_forum_message(message):
        return None, ACCESS_DENIED_TEXT
    channel = await authorizer.db.get_channel_by_group(message.chat.id)
    if channel is None:
        return None, "Эта супергруппа ещё не подключена. Используйте /setup."
    if not message.from_user:
        return None, ACCESS_DENIED_TEXT
    decision = await authorizer.require(
        actor_id=message.from_user.id,
        channel_id=int(channel["channel_id"]),
        action=ChannelAction.SETTINGS,
        context_group_id=message.chat.id,
        require_current_telegram_admin=True,
    )
    if not decision.allowed:
        return None, ACCESS_DENIED_TEXT
    return decision.channel, None


async def _panel_text(*, bot: Bot, db: Database, channel) -> str:
    channel_id = int(channel["channel_id"])
    subscribers = await db.count_channel_subscribers(channel_id)
    topics = await db.count_channel_topics(channel_id)
    link = await create_start_link(bot, f"ref_c_{channel_id}", encode=False)
    tz = ZoneInfo(str(channel["timezone_name"]))
    next_reset = dt_from_db(str(channel["next_reset_at"])).astimezone(tz)
    return await render_template(db, channel_id, "panel.overview",
        channel_name=str(channel["group_title"]), subscribers=subscribers, topics=topics,
        period_days=int(channel["reset_interval_days"]), timezone=str(channel["timezone_name"]),
        next_reset=next_reset.strftime("%d.%m.%Y %H:%M"), deep_link=link,
        notice_text=str(channel["notice_text"]))


async def authorize_sanction_target(
    *, message: Message, db: Database, channel_id: int, user_id: int,
    guard: AdminGuard | None = None, authorizer: ChannelAuthorizer | None = None,
) -> object | None:
    if not message.from_user or message.chat.type != ChatType.SUPERGROUP:
        return None
    if authorizer is not None:
        decision = await authorizer.require(
            actor_id=message.from_user.id, channel_id=channel_id,
            action=ChannelAction.MODERATION, context_group_id=message.chat.id,
            require_current_telegram_admin=True,
        )
        channel = decision.channel if decision.allowed else None
    else:
        if guard is None or not await guard.is_group_admin(message, message.chat.id):
            return None
        channel = await db.get_channel_by_id(channel_id)
    if channel is None or not bool(channel["enabled"]) or int(channel["group_id"]) != message.chat.id:
        return None
    topic = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id or 0)
    if topic is None or int(topic["channel_id"]) != channel_id or int(topic["user_id"]) != user_id:
        return None
    return channel


async def apply_sanction_from_flow(*, message: Message, db: Database, guard: AdminGuard, flow_data: dict[str, object], authorizer: ChannelAuthorizer | None = None) -> str | None:
    """The only future finalisation point for sanctions.

    Callback data and FSM storage are merely a request to act.  This function
    re-reads the channel/topic context and authorisation immediately before it
    delegates to the reason-aware database API.
    """
    if not sanction_flow_is_complete(flow_data):
        return None
    try:
        channel_id = int(flow_data["channel_id"])
        user_id = int(flow_data["target_user_id"])
        action = str(flow_data["sanction_type"])
        reason_choice = str(flow_data["reason_choice"])
        show_reason = flow_data["show_reason_to_subscriber"]
        Database.resolve_sanction_reason(
            reason_choice,
            flow_data.get("custom_reason") if isinstance(flow_data.get("custom_reason"), str) else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if message.from_user is None:
        return None
    if await authorize_sanction_target(
        message=message,
        db=db,
        guard=guard,
        authorizer=authorizer,
        channel_id=channel_id,
        user_id=user_id,
    ) is None:
        return None
    parameters = flow_data.get("sanction_parameters")
    if not isinstance(parameters, dict):
        return None
    seconds = _sanction_duration_seconds(action, parameters)
    if action in {"rate_limit", "mute", "temporary_block"} and not _valid_sanction_duration(action, seconds):
        return None
    if action in {"permanent_block", "warning"} and parameters:
        return None
    custom_reason = flow_data.get("custom_reason")
    kwargs: dict[str, object] = {}
    if action == "rate_limit": kwargs["rate_limit_seconds"] = seconds
    elif action in {"mute", "temporary_block"}: kwargs["duration_seconds"] = seconds
    return await db.apply_subscriber_sanction(
        channel_id=channel_id, user_id=user_id, admin_id=message.from_user.id,
        action=action, reason_choice=reason_choice,
        custom_reason=custom_reason if isinstance(custom_reason, str) else None,
        show_reason_to_subscriber=show_reason, **kwargs,
    )


async def _panel_callback_channel(callback: CallbackQuery, authorizer: ChannelAuthorizer, *, denied_template: str | None = None):
    if callback.message is None or callback.from_user is None:
        return None
    if callback.message.chat.type == ChatType.PRIVATE:
        channel = await authorizer.db.get_active_admin_channel(callback.from_user.id)
        context_group_id = None
    else:
        channel = await authorizer.db.get_channel_by_group(callback.message.chat.id)
        context_group_id = callback.message.chat.id
    if channel is None:
        await callback.answer(render_default(denied_template, {}) if denied_template else ACCESS_DENIED_TEXT, show_alert=True)
        return None
    decision = await authorizer.require(
        actor_id=callback.from_user.id, channel_id=int(channel["channel_id"]),
        action=ChannelAction.PANEL, context_group_id=context_group_id,
        require_current_telegram_admin=context_group_id is not None,
    )
    if not decision.allowed:
        await callback.answer(render_default(denied_template, {}) if denied_template else ACCESS_DENIED_TEXT, show_alert=True)
        return None
    return decision.channel


async def _require_private_owner_channel(
    *, authorizer: ChannelAuthorizer, actor_id: int, channel_id: int, action: ChannelAction,
):
    """Resolve a private owner action through the live Telegram membership gate.

    Persisted owner/channel selection is only a lookup hint.  Every sensitive
    private action must re-check that the stored owner is still a current
    administrator of the channel's Telegram supergroup.
    """
    decision = await authorizer.require(
        actor_id=actor_id,
        channel_id=channel_id,
        action=action,
    )
    return decision.channel if decision.allowed else None


async def _cleanup_text(db: Database, channel) -> str:
    enabled = "Включена" if bool(channel["auto_cleanup_enabled"]) else "Выключена"
    basis = "по последней активности" if channel["cleanup_basis"] == "last_activity_at" else "по дате создания"
    scope = "отвеченные и закрытые" if channel["cleanup_status_scope"] == "answered_closed" else "все обращения"
    actions = {"delete": "удалить", "close": "закрыть", "close_then_delete": "закрыть, затем удалить"}
    return await render_template(db, int(channel["channel_id"]), "cleanup.overview",
        enabled=enabled, period_days=int(channel["reset_interval_days"]), basis=basis, scope=scope,
        action=actions[str(channel["cleanup_action"])], final_delete_days=int(channel["cleanup_final_delete_days"]))


def register_handlers(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    runtime: FeedbackRuntime,
    cleaner: TopicCleaner,
    settings,
) -> None:
    router = Router(name="feedback")
    guard = AdminGuard(bot=bot)
    authorizer = ChannelAuthorizer(
        db=db, member_resolver=lambda group_id, user_id: telegram_group_admin_resolver(bot, group_id, user_id)
    )
    broadcast_runtime = BroadcastRuntime(bot=bot, db=db)
    reaction_runtime = ReactionRuntime(bot=bot, db=db, authorizer=authorizer)

    async def _persist_broadcast_source(
        *, state: FSMContext, channel_id: int, group_id: int, owner_id: int,
        message_ids: list[int], media_group_id: str | None,
    ) -> str | None:
        data = await state.get_data()
        if (
            data.get("channel_id") != channel_id
            or data.get("group_id") != group_id
            or data.get("owner_id") != owner_id
        ):
            return None
        broadcast_id = data.get("broadcast_id")
        if isinstance(broadcast_id, str):
            updated = await db.update_broadcast_draft_source(
                broadcast_id=broadcast_id, channel_id=channel_id, created_by=owner_id,
                source_chat_id=group_id, source_message_id=message_ids[0],
                source_message_ids=message_ids, source_media_group_id=media_group_id,
            )
            if not updated:
                return None
        else:
            draft = await db.create_broadcast_draft(
                channel_id=channel_id, created_by=owner_id, source_chat_id=group_id,
                source_message_id=message_ids[0], source_message_ids=message_ids,
                source_media_group_id=media_group_id,
            )
            broadcast_id = str(draft["broadcast_id"])
        await state.set_state(BroadcastFlow.confirmation)
        await state.update_data(broadcast_id=broadcast_id)
        return broadcast_id

    async def _flush_broadcast_album(items: list[BroadcastAlbumItem]) -> None:
        first = items[0]
        message = first.message
        state = first.state
        if len(items) < 2 or len(items) > 10:
            await message.answer(await render_template(db, first.channel_id, "broadcast.unsupported"))
            return
        if any(
            item.channel_id != first.channel_id
            or item.group_id != first.group_id
            or item.owner_id != first.owner_id
            or not broadcast_message_is_copyable(item.message)
            for item in items
        ):
            await message.answer(await render_template(db, first.channel_id, "broadcast.unsupported"))
            return
        data = await state.get_data()
        if (
            data.get("channel_id") != first.channel_id
            or data.get("group_id") != first.group_id
            or data.get("owner_id") != first.owner_id
        ):
            await message.answer(render_default("broadcast.unavailable", {}))
            return
        decision = await authorizer.require(
            actor_id=first.owner_id, channel_id=first.channel_id, action=ChannelAction.BROADCAST,
            context_group_id=first.group_id, require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await state.clear()
            await message.answer(render_default("broadcast.owner_required", {}))
            return
        message_ids = [item.message.message_id for item in items]
        try:
            preview = await bot.copy_messages(
                chat_id=first.group_id, from_chat_id=first.group_id, message_ids=message_ids,
            )
            if preview is not None and len(preview) != len(message_ids):
                raise RuntimeError("partial_album_preview")
        except Exception:
            await message.answer(await render_template(db, first.channel_id, "broadcast.unsupported"))
            return
        broadcast_id = await _persist_broadcast_source(
            state=state, channel_id=first.channel_id, group_id=first.group_id, owner_id=first.owner_id,
            message_ids=message_ids, media_group_id=str(message.media_group_id),
        )
        if broadcast_id is None:
            await state.clear()
            await message.answer(render_default("broadcast.unavailable", {}))
            return
        await message.answer(
            await render_template(db, first.channel_id, "broadcast.preview_ready"),
            reply_markup=broadcast_preview_keyboard(broadcast_id),
        )

    broadcast_albums = BroadcastAlbumCollector(
        delay=max(float(getattr(settings, "media_group_delay", 0.8)), 0.2),
        callback=_flush_broadcast_album,
    )
    dispatcher.shutdown.register(broadcast_albums.close)
    # Prevent a retry of the same Telegram update from producing duplicate
    # restriction notices, while keeping ordinary notifications for new tries.
    restriction_notice_updates: set[tuple[int, int, int]] = set()

    # --------------------------------------------------------------
    # /setup
    # --------------------------------------------------------------

    async def _finish_setup(
        *,
        message: Message,
        owner_id: int,
        group_id: int,
        group_title: str,
        bot_is_creator: bool,
        bot_can_delete_messages: bool,
        anonymous_prefix: str | None = None,
        state: FSMContext | None = None,
    ) -> bool:
        kwargs = dict(
            owner_id=owner_id,
            group_id=group_id,
            group_title=group_title,
            default_reset_days=settings.default_reset_days,
            default_notice_text=settings.default_notice_text,
            default_timezone=settings.default_timezone,
        )
        if anonymous_prefix is not None:
            kwargs["anonymous_prefix"] = anonymous_prefix
        try:
            status, channel = await db.register_channel(**kwargs)
        except (TypeError, ValueError):
            logger.exception("Setup registration rejected invalid input")
            await message.answer(render_default("setup.anonymous_prefix_invalid", {}))
            return False
        except Exception:
            logger.exception("Setup registration failed")
            await message.answer(render_default("setup.failed", {}))
            return False
        if status == "owner_channel_limit":
            if state is not None:
                await state.clear()
            await message.answer(render_default("setup.channel_limit", {}))
            return False
        if status == "group_has_other_owner":
            if state is not None:
                await state.clear()
            await message.answer(render_default("setup.owner_conflict", {}))
            return False
        if channel is None:
            await message.answer(render_default("setup.failed", {}))
            return False
        channel_id = int(channel["channel_id"])
        try:
            await sync_command_menus(bot=bot, db=db)
            link = await create_start_link(bot, f"ref_c_{channel_id}", encode=False)
        except TelegramAPIError:
            await message.answer(await render_template(db, channel_id, "setup.failed"))
            return False
        warning = ""
        if not (bot_is_creator or bot_can_delete_messages):
            warning = await render_template(db, channel_id, "setup.warning_delete_permission")
        success_key = "setup.success.created" if status == "created" else "setup.success.existing"
        if state is not None:
            await state.clear()
        await message.answer(
            await render_template(
                db,
                channel_id,
                success_key,
                channel_name=str(channel["group_title"]),
                deep_link=link,
                warning=warning,
            ),
            disable_web_page_preview=True,
        )
        return True

    @router.message(Command("setup"))
    async def setup_handler(message: Message, state: FSMContext) -> None:
        # No channel exists yet: every rejection below is a global safe default.
        if message.chat.type != ChatType.SUPERGROUP:
            await message.answer(render_default("setup.supergroup_required", {})); return
        if not is_general_forum_message(message):
            await message.answer(render_default("setup.topic_context_invalid", {})); return
        try:
            chat = await bot.get_chat(message.chat.id)
        except TelegramAPIError:
            await message.answer(render_default("setup.failed", {})); return
        if not getattr(chat, "is_forum", False):
            await message.answer(render_default("setup.forum_required", {})); return
        if not message.from_user:
            await message.answer(render_default("setup.anonymous_caller", {})); return
        try:
            caller = await bot.get_chat_member(message.chat.id, message.from_user.id)
            me = await bot.get_me()
            bot_member = await bot.get_chat_member(message.chat.id, me.id)
        except TelegramAPIError:
            await message.answer(render_default("setup.failed", {})); return
        if caller.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer(render_default("setup.caller_not_admin", {})); return
        bot_is_creator = bot_member.status == ChatMemberStatus.CREATOR
        bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if not bot_is_creator and not bot_is_admin:
            await message.answer(render_default("setup.bot_not_admin", {})); return
        if not bot_is_creator and not bool(getattr(bot_member, "can_manage_topics", False)):
            await message.answer(render_default("setup.bot_missing_topics", {})); return

        owner_id = message.from_user.id
        existing = await db.get_channel_by_group(message.chat.id)
        if existing is not None:
            if int(existing["owner_id"]) != owner_id:
                await message.answer(render_default("setup.owner_conflict", {})); return
            await state.clear()
            await _finish_setup(
                message=message,
                owner_id=owner_id,
                group_id=message.chat.id,
                group_title=message.chat.title or str(message.chat.id),
                bot_is_creator=bot_is_creator,
                bot_can_delete_messages=bool(getattr(bot_member, "can_delete_messages", False)),
            )
            return

        if len(await db.list_enabled_channels_for_owner(owner_id)) >= 5:
            await message.answer(render_default("setup.channel_limit", {})); return

        # A new channel is not persisted until a valid prefix is supplied.  This
        # keeps initial setup atomic: abandoning the prompt does not leave a
        # half-configured channel with an accidental default prefix.
        await state.clear()
        await state.set_state(SetupFlow.anonymous_prefix)
        await state.update_data(
            setup_group_id=message.chat.id,
            setup_owner_id=owner_id,
            setup_group_title=message.chat.title or str(message.chat.id),
        )
        await message.answer(render_default("setup.anonymous_prefix_prompt", {}))

    @router.message(SetupFlow.anonymous_prefix)
    async def setup_anonymous_prefix_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        owner_id = data.get("setup_owner_id")
        group_id = data.get("setup_group_id")
        group_title = data.get("setup_group_title")
        if (
            message.chat.type != ChatType.SUPERGROUP
            or not message.from_user
            or not isinstance(owner_id, int)
            or not isinstance(group_id, int)
            or owner_id != message.from_user.id
            or group_id != message.chat.id
            or not isinstance(group_title, str)
        ):
            await state.clear()
            await message.answer(render_default("setup.failed", {}))
            return
        if message.message_thread_id:
            existing_topic = await db.get_topic_by_group_thread(
                group_id=message.chat.id, topic_id=message.message_thread_id
            )
            if existing_topic is not None:
                await message.answer(render_default("setup.topic_context_invalid", {}))
                return
        try:
            prefix = db.normalize_anonymous_prefix(message.text or "")
        except ValueError:
            await message.answer(render_default("setup.anonymous_prefix_invalid", {}))
            return

        # Permissions can change while the FSM waits for the prefix. Re-check
        # them immediately before creating the persistent channel record.
        try:
            chat = await bot.get_chat(group_id)
            caller = await bot.get_chat_member(group_id, owner_id)
            me = await bot.get_me()
            bot_member = await bot.get_chat_member(group_id, me.id)
        except TelegramAPIError:
            await message.answer(render_default("setup.failed", {}))
            return
        if not getattr(chat, "is_forum", False):
            await state.clear()
            await message.answer(render_default("setup.forum_required", {}))
            return
        if caller.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await state.clear()
            await message.answer(render_default("setup.caller_not_admin", {}))
            return
        bot_is_creator = bot_member.status == ChatMemberStatus.CREATOR
        bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if not bot_is_creator and not bot_is_admin:
            await state.clear()
            await message.answer(render_default("setup.bot_not_admin", {}))
            return
        if not bot_is_creator and not bool(getattr(bot_member, "can_manage_topics", False)):
            await state.clear()
            await message.answer(render_default("setup.bot_missing_topics", {}))
            return

        await _finish_setup(
            message=message,
            owner_id=owner_id,
            group_id=group_id,
            group_title=group_title,
            bot_is_creator=bot_is_creator,
            bot_can_delete_messages=bool(getattr(bot_member, "can_delete_messages", False)),
            anonymous_prefix=prefix,
            state=state,
        )

    # --------------------------------------------------------------
    # Channel panel and settings
    # --------------------------------------------------------------

    @router.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
        if error:
            await message.answer(render_default("statistics.unavailable", {}))
            return
        if not bool(channel["enabled"]):
            await message.answer(await render_template(db, int(channel["channel_id"]), "statistics.unavailable"))
            return
        stats = await db.get_channel_statistics(int(channel["channel_id"]), period="all")
        await message.answer(
            await render_statistics_page(db=db, channel_id=int(channel["channel_id"]), stats=stats, page="overview"),
            reply_markup=statistics_keyboard(source="stats"),
        )

    @router.callback_query(F.data.startswith("stats:"))
    async def statistics_callback(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        data = (callback.data or "").split(":")
        if data != ["stats", "back"] and (len(data) != 3 or data[1] not in STATISTICS_PAGES or data[2] not in STATISTICS_PERIODS):
            await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
            return
        channel = await _statistics_callback_channel(callback, authorizer, source="stats")
        if channel is None:
            return
        try:
            if data == ["stats", "back"]:
                await callback.message.edit_reply_markup(reply_markup=None)
            else:
                if data[1] == "admins":
                    stats = await db.get_channel_admin_statistics(int(channel["channel_id"]), period=data[2])
                    text = await render_statistics_page(db=db, channel_id=int(channel["channel_id"]), stats=stats, page="admins")
                else:
                    stats = await db.get_channel_statistics(int(channel["channel_id"]), period=data[2])
                    text = await render_statistics_page(db=db, channel_id=int(channel["channel_id"]), stats=stats, page=data[1])
                await callback.message.edit_text(text, reply_markup=statistics_keyboard(source="stats", page=data[1], period=data[2]))
        except TelegramBadRequest:
            await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
            return
        await callback.answer()

    @router.message(Command("panel"))
    async def panel_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not message.from_user: return
        if message.chat.type == ChatType.PRIVATE:
            stored_channels = await db.list_enabled_channels_for_owner(message.from_user.id)
            channels = []
            for candidate in stored_channels:
                allowed = await _require_private_owner_channel(
                    authorizer=authorizer, actor_id=message.from_user.id,
                    channel_id=int(candidate["channel_id"]), action=ChannelAction.PANEL,
                )
                if allowed is not None:
                    channels.append(allowed)
            if not channels:
                await message.answer(render_default("panel.no_channels", {})); return
            if len(channels) > 1:
                await message.answer(render_default("panel.choose_channel", {}), reply_markup=admin_channel_selection_keyboard(channels)); return
            channel = channels[0]
            await db.set_active_admin_channel(owner_id=message.from_user.id, channel_id=int(channel["channel_id"]))
        else:
            channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
            if error:
                await message.answer(render_default("panel.unavailable", {})); return
        await message.answer(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=panel_keyboard(), disable_web_page_preview=True)

    @router.message(Command("set_period"))
    async def set_period_handler(message: Message, command: CommandObject) -> None:
        channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
        if error:
            await message.answer(render_default("panel.unavailable", {})); return
        channel_id = int(channel["channel_id"])
        try:
            days = int((command.args or "").strip())
            if not 2 <= days <= 3650: raise ValueError
        except ValueError:
            await message.answer(await render_template(db, channel_id, "settings.period_usage")); return
        next_reset = await db.set_channel_period(channel_id, days)
        local_reset = next_reset.astimezone(ZoneInfo(str(channel["timezone_name"])))
        await message.answer(await render_template(db, channel_id, "settings.period_saved", days=days, next_reset=local_reset.strftime("%d.%m.%Y %H:%M")))

    @router.message(Command("set_announcement"))
    async def set_announcement_handler(message: Message, command: CommandObject) -> None:
        channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
        if error:
            await message.answer(render_default("panel.unavailable", {})); return
        channel_id, text = int(channel["channel_id"]), (command.args or "").strip()
        if not text:
            await message.answer(await render_template(db, channel_id, "settings.notice_usage")); return
        if len(text) > 4000:
            await message.answer(await render_template(db, channel_id, "settings.notice_too_long")); return
        await db.set_channel_notice(channel_id, text)
        await message.answer(await render_template(db, channel_id, "settings.notice_saved"))

    @router.message(Command("set_topic_template"))
    async def set_topic_template_handler(message: Message, command: CommandObject) -> None:
        channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
        if error:
            await message.answer(render_default("panel.unavailable", {})); return
        channel_id, parts = int(channel["channel_id"]), (command.args or "").strip().split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"identified", "anonymous"}:
            await message.answer(await render_template(db, channel_id, "settings.topic_template_usage")); return
        mode, template = parts
        try:
            validate_topic_template(template, privacy_mode=mode)
        except ValueError as exc:
            allowed = "{anonymous_tag}" if mode == "anonymous" else "{name}, {username}, {user_id}"
            await message.answer(await render_template(db, channel_id, "settings.topic_template_invalid", error=str(exc), allowed=allowed)); return
        await db.set_channel_topic_template(channel_id=channel_id, privacy_mode=mode, template=template)
        await message.answer(await render_template(db, channel_id, "settings.topic_template_saved"))

    @router.message(Command("set_timezone"))
    async def set_timezone_handler(message: Message, command: CommandObject) -> None:
        channel, error = await _owner_channel_in_group(message=message, authorizer=authorizer)
        if error:
            await message.answer(render_default("panel.unavailable", {})); return
        channel_id, timezone_name = int(channel["channel_id"]), (command.args or "").strip()
        if not timezone_name:
            await message.answer(await render_template(db, channel_id, "settings.timezone_usage")); return
        try: ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            await message.answer(await render_template(db, channel_id, "settings.timezone_invalid")); return
        await db.set_channel_timezone(channel_id, timezone_name)
        await message.answer(await render_template(db, channel_id, "settings.timezone_saved", timezone=timezone_name))

    @router.message(Command("search"), F.chat.type == ChatType.PRIVATE)
    async def search_command(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        channel = await db.get_active_admin_channel(message.from_user.id)
        if channel is not None:
            channel = await _require_private_owner_channel(
                authorizer=authorizer, actor_id=message.from_user.id,
                channel_id=int(channel["channel_id"]), action=ChannelAction.SEARCH,
            )
        if channel is None or not bool(channel["enabled"]):
            await message.answer(render_default("search.unavailable", {}))
            return
        await state.clear()
        await state.set_state(SearchFlow.query)
        await state.update_data(search_channel_id=int(channel["channel_id"]))
        await message.answer(await render_template(db, int(channel["channel_id"]), "search.prompt"))

    # --------------------------------------------------------------
    # Panel callbacks
    # --------------------------------------------------------------
    @router.callback_query(F.data.startswith("panel:select:"))
    async def panel_select_channel(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None or callback.from_user is None or callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer(render_default("panel.unavailable", {}), show_alert=True); return
        try: channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(render_default("panel.unavailable", {}), show_alert=True); return
        await state.clear()
        channel = await _require_private_owner_channel(
            authorizer=authorizer, actor_id=callback.from_user.id,
            channel_id=channel_id, action=ChannelAction.PANEL,
        )
        if channel is None or not await db.set_active_admin_channel(owner_id=callback.from_user.id, channel_id=channel_id):
            await callback.answer(render_default("panel.unavailable", {}), show_alert=True); return
        channel = await db.get_active_admin_channel(callback.from_user.id)
        await callback.message.edit_text(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=panel_keyboard(), disable_web_page_preview=True)
        await callback.answer(await render_template(db, channel_id, "panel.channel_selected"))

    @router.callback_query(F.data == "panel:search")
    async def panel_search_start(callback: CallbackQuery, state: FSMContext) -> None:
        channel = await _panel_callback_channel(callback, authorizer)
        if channel is None or callback.message is None or not bool(channel["enabled"]):
            return
        await state.clear(); await state.set_state(SearchFlow.query)
        await state.update_data(search_channel_id=int(channel["channel_id"]))
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "search.prompt"))
        await callback.answer()

    @router.message(SearchFlow.query)
    async def panel_search_query(message: Message, state: FSMContext) -> None:
        data=await state.get_data(); channel_id=data.get("search_channel_id")
        if not message.from_user or not isinstance(channel_id,int): await state.clear(); return
        channel=await db.get_active_admin_channel(message.from_user.id)
        if channel is not None and int(channel["channel_id"]) == channel_id:
            channel = await _require_private_owner_channel(
                authorizer=authorizer, actor_id=message.from_user.id,
                channel_id=channel_id, action=ChannelAction.SEARCH,
            )
        query=(message.text or "").strip()
        if channel is None or int(channel["channel_id"])!=channel_id or not bool(channel["enabled"]):
            await state.clear(); await message.answer(await render_template(db, channel_id, "search.unavailable")); return
        if not query or len(query)>96: await message.answer(await render_template(db, channel_id, "search.invalid_query")); return
        rows,total=await db.search_subscribers(channel_id=channel_id,query=query)
        await state.update_data(search_query=query, search_page=0)
        await render_search_results(message=message, channel_id=channel_id, rows=rows, total=total, page=0)

    def forum_topic_url(group_id: object, topic_id: object) -> str | None:
        """Return a Telegram forum-topic link only for a validated supergroup ID."""
        if not isinstance(group_id, int) or not isinstance(topic_id, int) or topic_id < 1:
            return None
        raw = str(group_id)
        if not raw.startswith("-100") or len(raw) <= 4:
            return None
        return f"https://t.me/c/{raw[4:]}/{topic_id}"

    async def search_results_view(*, channel_id: int, rows: list[dict[str, object]], total: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        if not rows:
            return await render_template(db, channel_id, "search.empty"), InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Новый поиск", callback_data="panel:search")
            ], [InlineKeyboardButton(text="Назад", callback_data="panel:home")]])
        lines = [await render_template(db, channel_id, "search.results", count=total)]
        result_buttons = []
        for index, row in enumerate(rows):
            status = f" — {html.escape(str(row['status']))}" if row['status'] else ""
            lines.append(f"• {html.escape(str(row['display_name']))}{status}")
            buttons = [InlineKeyboardButton(text=f"Результат {index + 1}", callback_data=f"search:open:{index}")]
            topic_url = forum_topic_url(row.get("group_id"), row.get("topic_id"))
            if topic_url:
                buttons.append(InlineKeyboardButton(text="Открыть", url=topic_url))
            result_buttons.append(buttons)
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="◀", callback_data="search:page:prev"))
        if (page + 1) * 8 < total:
            nav.append(InlineKeyboardButton(text="▶", callback_data="search:page:next"))
        keyboard = result_buttons + [[InlineKeyboardButton(text="Новый поиск", callback_data="panel:search")]]
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(text="Назад", callback_data="panel:home")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def render_search_results(*, message: Message, channel_id: int, rows: list[dict[str, object]], total: int, page: int) -> None:
        text, keyboard = await search_results_view(channel_id=channel_id, rows=rows, total=total, page=page)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("search:page:"))
    async def search_page(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id, query = data.get("search_channel_id"), data.get("search_query")
        try:
            page = int(data.get("search_page", 0))
        except (TypeError, ValueError):
            page = -1
        if callback.message is None or callback.from_user is None or not isinstance(channel_id, int) or not isinstance(query, str) or page < 0:
            await state.clear()
            await callback.answer(render_default("search.stale", {}), show_alert=True)
            return
        channel = await db.get_active_admin_channel(callback.from_user.id)
        if channel is not None and int(channel["channel_id"]) == channel_id:
            channel = await _require_private_owner_channel(
                authorizer=authorizer, actor_id=callback.from_user.id,
                channel_id=channel_id, action=ChannelAction.SEARCH,
            )
        if channel is None or int(channel["channel_id"]) != channel_id or not bool(channel["enabled"]):
            await state.clear()
            await callback.answer(render_default("search.unavailable", {}), show_alert=True)
            return
        direction = (callback.data or "").rsplit(":", 1)[-1]
        if direction not in {"prev", "next"}:
            await callback.answer(await render_template(db, channel_id, "search.invalid_callback"), show_alert=True)
            return
        page = max(0, page + (-1 if direction == "prev" else 1))
        rows, total = await db.search_subscribers(channel_id=channel_id, query=query, offset=page * 8)
        if not rows and page:
            page -= 1
            rows, total = await db.search_subscribers(channel_id=channel_id, query=query, offset=page * 8)
        await state.update_data(search_page=page)
        text, keyboard = await search_results_view(channel_id=channel_id, rows=rows, total=total, page=page)
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("search:open:"))
    async def search_open_result(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id, query = data.get("search_channel_id"), data.get("search_query")
        try:
            page, index = int(data.get("search_page", 0)), int((callback.data or "").rsplit(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            page, index = -1, -1
        if callback.message is None or callback.from_user is None or not isinstance(channel_id, int) or not isinstance(query, str) or not 0 <= index < 8 or page < 0:
            await state.clear()
            await callback.answer(render_default("search.stale", {}), show_alert=True)
            return
        channel = await db.get_active_admin_channel(callback.from_user.id)
        if channel is not None and int(channel["channel_id"]) == channel_id:
            channel = await _require_private_owner_channel(
                authorizer=authorizer, actor_id=callback.from_user.id,
                channel_id=channel_id, action=ChannelAction.SEARCH,
            )
        if channel is None or int(channel["channel_id"]) != channel_id or not bool(channel["enabled"]):
            await state.clear()
            await callback.answer(render_default("search.unavailable", {}), show_alert=True)
            return
        rows, _ = await db.search_subscribers(channel_id=channel_id, query=query, offset=page * 8)
        if index >= len(rows):
            await callback.answer(await render_template(db, channel_id, "search.result_unavailable"), show_alert=True)
            return
        row = rows[index]
        # State and callback only provide a lookup key. Re-check that the target is
        # still attached to the selected channel before exposing any metadata.
        card = await db.get_subscriber_card_data(channel_id=channel_id, user_id=int(row["user_id"]), privacy_mode=str(row["privacy_mode"]))
        if card is None:
            await state.clear()
            await callback.answer(await render_template(db, channel_id, "search.result_unavailable"), show_alert=True)
            return
        await state.clear()
        title = html.escape(str(row["display_name"]))
        if row["topic_id"] is None:
            text = await render_template(db, channel_id, "search.topic_unavailable", display_name=str(row["display_name"]))
        else:
            text = await render_template(db, channel_id, "search.open_result", display_name=str(row["display_name"]))
        await callback.message.answer(text)
        await callback.answer()

    @router.callback_query(F.data.startswith("panel:"))
    async def panel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        data = callback.data or ""
        denied_template = ("export.unavailable" if data.startswith("panel:export")
                           else "statistics.unavailable" if data == "panel:stats" or data.startswith("panel:stats:")
                           else None)
        channel = await _panel_callback_channel(callback, authorizer, denied_template=denied_template)
        if channel is None or callback.message is None:
            return
        channel_id = int(channel["channel_id"])
        if data.startswith("panel:export"):
            if not bool(channel["enabled"]):
                await callback.answer(render_default("export.unavailable", {}), show_alert=True)
                return
            parts = data.split(":")
            if len(parts) == 3 and parts[2] in STATISTICS_PERIODS:
                period = parts[2]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="CSV", callback_data=f"panel:exportfile:csv:{period}"),
                    InlineKeyboardButton(text="XLSX", callback_data=f"panel:exportfile:xlsx:{period}"),
                ], [InlineKeyboardButton(text="Назад", callback_data=f"panel:stats:overview:{period}")]])
                await callback.message.edit_text(await render_template(db, channel_id, "export.choose_format"), reply_markup=keyboard)
                await callback.answer(); return
            if len(parts) == 4 and parts[1] == "exportfile" and parts[2] in {"csv", "xlsx"} and parts[3] in STATISTICS_PERIODS:
                await callback.message.answer(await render_template(db, channel_id, "export.preparing"))
                try:
                    snapshot = await db.get_channel_export_snapshot(channel_id, period=parts[3])
                    payload = csv_export(snapshot) if parts[2] == "csv" else xlsx_export(snapshot)
                except Exception:
                    logger.exception("Export generation failed")
                    await callback.answer(await render_template(db, channel_id, "export.failed"), show_alert=True)
                    return
                if len(payload) > 45 * 1024 * 1024:
                    await callback.answer(await render_template(db, channel_id, "export.too_large"), show_alert=True)
                    return
                filename = f"statistics_channel_{channel_id}_{parts[3]}.{parts[2]}"
                try:
                    await callback.message.answer_document(BufferedInputFile(payload, filename=filename))
                    await callback.message.answer(await render_template(db, channel_id, "export.sent"))
                except TelegramAPIError:
                    logger.exception("Export delivery failed")
                    await callback.answer(await render_template(db, channel_id, "export.delivery_failed"), show_alert=True)
                    return
                await callback.answer()
                return
            await callback.answer(await render_template(db, channel_id, "export.unavailable"), show_alert=True)
            return
        if data in {"panel:home", "panel:refresh"}:
            if data == "panel:home":
                await state.clear()
            await callback.message.edit_text(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=panel_keyboard(), disable_web_page_preview=True)
        elif data == "panel:stats" or data.startswith("panel:stats:"):
            parts = data.split(":")
            if data == "panel:stats":
                page, period = "overview", "all"
            elif len(parts) == 4 and parts[2] in STATISTICS_PAGES and parts[3] in STATISTICS_PERIODS:
                page, period = parts[2], parts[3]
            else:
                await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
                return
            if not bool(channel["enabled"]):
                await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
                return
            try:
                if page == "admins":
                    stats = await db.get_channel_admin_statistics(channel_id, period=period)
                    text = await render_statistics_page(db=db, channel_id=int(channel["channel_id"]), stats=stats, page="admins")
                else:
                    stats = await db.get_channel_statistics(channel_id, period=period)
                    text = await render_statistics_page(db=db, channel_id=channel_id, stats=stats, page=page)
                await callback.message.edit_text(text, reply_markup=statistics_keyboard(source="panel", page=page, period=period))
            except TelegramBadRequest:
                await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
                return
        elif data == "panel:anonymous":
            counter = await db.get_anonymous_counter_state(channel_id)
            if counter is None:
                await callback.answer(render_default("panel.unavailable", {}), show_alert=True)
                return
            await callback.message.edit_text(
                await render_template(db, channel_id, "settings.anonymous_overview", prefix=str(counter["anonymous_prefix"]), next_number=int(counter["next_number"])),
                reply_markup=anonymous_settings_keyboard(),
            )
        elif data == "panel:anonymous:edit":
            if callback.message.chat.type != ChatType.PRIVATE:
                await callback.answer(await render_template(db, channel_id, "settings.anonymous_private_required"), show_alert=True)
                return
            await state.clear()
            await state.set_state(ChannelSettingsFlow.anonymous_prefix)
            await state.update_data(settings_channel_id=channel_id)
            await callback.message.answer(await render_template(db, channel_id, "settings.anonymous_edit_prompt"))
        elif data == "panel:reactions" or data.startswith("panel:reactions:"):
            if not is_general_forum_message(callback.message):
                await callback.answer(render_default("reaction.general_required", {}), show_alert=True)
                return
            reaction_decision = await authorizer.require(
                actor_id=callback.from_user.id, channel_id=channel_id,
                action=ChannelAction.REACTION_SETTINGS, context_group_id=callback.message.chat.id,
                require_current_telegram_admin=True,
            )
            if not reaction_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            settings = await db.get_channel_reaction_settings(channel_id)
            if data == "panel:reactions":
                await callback.message.edit_text(
                    await reaction_settings_text(db, channel_id),
                    reply_markup=reaction_settings_keyboard(settings),
                )
            else:
                action = data.rsplit(":", 1)[1]
                if action == "mode1":
                    await db.set_channel_reaction_mode(
                        channel_id=channel_id, mode="subscriber", updated_by=callback.from_user.id
                    )
                    settings = await db.get_channel_reaction_settings(channel_id)
                    await callback.message.edit_text(
                        await render_template(db, channel_id, "reaction.mode_subscriber_set"),
                        reply_markup=reaction_settings_keyboard(settings),
                    )
                elif action == "mode2" and settings.get("service_topic_id") is not None and not bool(settings.get("requires_repair")):
                    await db.set_channel_reaction_mode(
                        channel_id=channel_id, mode="service", updated_by=callback.from_user.id
                    )
                    settings = await db.get_channel_reaction_settings(channel_id)
                    await callback.message.edit_text(
                        await render_template(
                            db, channel_id, "reaction.mode_service_set",
                            topic=str(settings.get("service_topic_name") or "служебная ветка"),
                        ),
                        reply_markup=reaction_settings_keyboard(settings),
                    )
                elif action in {"mode2", "create", "recreate", "rename"}:
                    if action == "rename" and settings.get("service_topic_id") is None:
                        await callback.answer(await render_template(db, channel_id, "reaction.topic_failed"), show_alert=True)
                        return
                    flow_action = action
                    if action == "mode2":
                        flow_action = "recreate" if settings.get("service_topic_id") is not None else "create"
                    await state.clear()
                    await state.set_state(ReactionSettingsFlow.topic_name)
                    await state.update_data(
                        reaction_channel_id=channel_id, reaction_group_id=callback.message.chat.id,
                        reaction_topic_action=flow_action,
                    )
                    await callback.message.answer(await render_template(db, channel_id, "reaction.topic_name_prompt"))
                else:
                    await callback.answer(await render_template(db, channel_id, "reaction.topic_failed"), show_alert=True)
                    return
        elif data == "panel:prestart":
            description = await effective_prestart_description(bot, db)
            stored = await db.get_bot_prestart_card()
            media_state = "сохранено для предпросмотра" if stored is not None and stored["media_type"] else "нет"
            await callback.message.edit_text(
                await render_template(db, channel_id, "prestart.overview", description=description, media_state=media_state),
                reply_markup=prestart_card_keyboard(),
            )
        elif data == "panel:prestart:text":
            if callback.message.chat.type != ChatType.PRIVATE:
                await callback.answer(await render_template(db, channel_id, "prestart.private_required"), show_alert=True); return
            await state.clear(); await state.set_state(PreStartCardFlow.description); await state.update_data(prestart_auth_channel_id=channel_id)
            await callback.message.answer(await render_template(db, channel_id, "prestart.text_prompt"))
        elif data == "panel:prestart:media":
            if callback.message.chat.type != ChatType.PRIVATE:
                await callback.answer(await render_template(db, channel_id, "prestart.private_required"), show_alert=True); return
            await state.clear(); await state.set_state(PreStartCardFlow.media); await state.update_data(prestart_auth_channel_id=channel_id)
            await callback.message.answer(await render_template(db, channel_id, "prestart.media_prompt"))
        elif data == "panel:prestart:preview":
            await send_prestart_preview(message=callback.message, bot=bot, db=db)
        elif data == "panel:prestart:media_apply":
            stored = await db.get_bot_prestart_card()
            if stored is None or not stored["media_type"] or not stored["media_file_id"]:
                await callback.answer(await render_template(db, channel_id, "prestart.media_missing"), show_alert=True); return
            media_type, media_file_id = validate_media(str(stored["media_type"]), str(stored["media_file_id"]))
            instruction = description_picture_apply_instructions(media_type)
            try:
                if media_type == "photo":
                    await callback.message.answer_photo(photo=media_file_id, caption=instruction, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]))
                elif media_type == "video":
                    await callback.message.answer_video(video=media_file_id, caption=instruction, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]))
                else:
                    await callback.message.answer_animation(animation=media_file_id, caption=instruction, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]))
            except TelegramAPIError:
                await callback.answer(await render_template(db, channel_id, "prestart.media_stale"), show_alert=True); return
        elif data == "panel:prestart:media_remove":
            await db.remove_bot_prestart_media(updated_by=callback.from_user.id)
            await callback.message.answer(
                description_picture_remove_instructions(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]),
            )
            await callback.answer(await render_template(db, channel_id, "prestart.media_removed")); return
        elif data == "panel:prestart:reset":
            try:
                await apply_description(bot, DEFAULT_PRESTART_DESCRIPTION)
            except TelegramAPIError:
                await callback.answer(await render_template(db, channel_id, "prestart.reset_failed"), show_alert=True); return
            await db.reset_bot_prestart_card()
            await callback.message.answer(
                description_picture_remove_instructions(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]),
            )
            await callback.answer(await render_template(db, channel_id, "prestart.reset_done")); return
        elif data == "panel:notices":
            await callback.message.edit_text(await render_template(db, channel_id, "panel.notices", notice_text=str(channel["notice_text"])), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="panel:home")]]))
        elif data == "panel:texts":
            buttons = [[InlineKeyboardButton(text=category, callback_data=f"template:category:{index}:0")] for index, category in enumerate(template_categories())]
            buttons.append([InlineKeyboardButton(text="Сбросить все тексты", callback_data="template:reset_all")])
            buttons.append([InlineKeyboardButton(text="Назад", callback_data="panel:home")])
            await callback.message.edit_text(await render_template(db, channel_id, "panel.texts"), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        elif data == "panel:cleanup":
            await callback.message.edit_text(await _cleanup_text(db, channel), reply_markup=cleanup_keyboard(channel))
        elif data == "panel:cleanup:disable":
            await db.set_auto_cleanup_enabled(channel_id, False)
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=cleanup_keyboard(updated))
        elif data == "panel:cleanup:enable_menu":
            await callback.message.edit_text(await render_template(db, channel_id, "cleanup.enable_prompt"), reply_markup=cleanup_enable_keyboard())
        elif data.startswith("panel:cleanup:enable:"):
            days = int(data.rsplit(":", 1)[1])
            await db.enable_auto_cleanup(channel_id=channel_id, days=days)
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=cleanup_keyboard(updated))
        elif data.startswith("panel:cleanup:basis:") or data.startswith("panel:cleanup:scope:") or data.startswith("panel:cleanup:action:"):
            _, _, kind, value = data.split(":", 3)
            basis, scope, action = str(channel["cleanup_basis"]), str(channel["cleanup_status_scope"]), str(channel["cleanup_action"])
            if kind == "basis": basis = value
            elif kind == "scope": scope = value
            else: action = value
            await db.set_channel_cleanup_policy(channel_id=channel_id, basis=basis, status_scope=scope, action=action, final_delete_days=int(channel["cleanup_final_delete_days"]))
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=cleanup_keyboard(updated))
        elif data == "panel:manual_cleanup_preview":
            rows = await db.topics_created_before(channel_id=channel_id, cutoff=_manual_cutoff(str(channel["timezone_name"])))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить очистку", callback_data="panel:manual_cleanup_confirm")],
                [InlineKeyboardButton(text="Очистить + сбросить нумерацию", callback_data="panel:manual_cleanup_confirm_reset_anon")],
                [InlineKeyboardButton(text="Назад", callback_data="panel:home")],
            ])
            await callback.message.edit_text(await render_template(db, channel_id, "cleanup.manual_preview", count=len(rows)), reply_markup=keyboard)
        elif data in {"panel:manual_cleanup_confirm", "panel:manual_cleanup_confirm_reset_anon"}:
            result = await cleaner.cleanup_created_before(channel_id=channel_id, cutoff=_manual_cutoff(str(channel["timezone_name"])))
            reset_anon = data.endswith("reset_anon")
            if reset_anon and result["failed"] == 0:
                await db.reset_anonymous_cycle(channel_id)
                key = "cleanup.manual_complete_reset"
            elif reset_anon:
                key = "cleanup.manual_reset_skipped"
            else:
                key = "cleanup.manual_complete"
            values = {"deleted": result["deleted"], "closed": result["closed"], "failed": result["failed"]}
            await callback.message.edit_text(
                await render_template(db, channel_id, key, **values),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="panel:home")]]),
            )
        else:
            await callback.answer(await render_template(db, channel_id, "panel.unavailable"), show_alert=True)
            return
        await callback.answer()


    @router.message(ReactionSettingsFlow.topic_name)
    async def reaction_topic_name_handler(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id = data.get("reaction_channel_id")
        group_id = data.get("reaction_group_id")
        action = data.get("reaction_topic_action")
        if (
            message.from_user is None or not isinstance(channel_id, int) or not isinstance(group_id, int)
            or action not in {"create", "recreate", "rename"}
            or message.chat.id != group_id or not is_general_forum_message(message)
        ):
            await state.clear()
            await message.answer(render_default("reaction.general_required", {}))
            return
        decision = await authorizer.require(
            actor_id=message.from_user.id, channel_id=channel_id,
            action=ChannelAction.REACTION_SETTINGS, context_group_id=group_id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await state.clear()
            await message.answer(ACCESS_DENIED_TEXT)
            return
        topic_name = " ".join((message.text or "").strip().split())
        if not 1 <= len(topic_name) <= 128:
            await message.answer(await render_template(db, channel_id, "reaction.topic_name_invalid"))
            return
        try:
            if action == "rename":
                settings = await db.get_channel_reaction_settings(channel_id)
                service_topic_id = settings.get("service_topic_id")
                if not isinstance(service_topic_id, int):
                    raise ValueError("Service topic is missing")
                await bot.edit_forum_topic(
                    chat_id=group_id, message_thread_id=service_topic_id, name=topic_name
                )
                if not await db.rename_reaction_service_topic(
                    channel_id=channel_id, topic_name=topic_name, updated_by=message.from_user.id
                ):
                    raise ValueError("Service topic is missing")
                result_key = "reaction.topic_renamed"
            else:
                topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
                await db.set_reaction_service_topic(
                    channel_id=channel_id, topic_id=int(topic.message_thread_id),
                    topic_name=topic_name, updated_by=message.from_user.id, activate=True,
                )
                result_key = "reaction.topic_created"
        except TelegramAPIError as exc:
            if is_missing_or_closed_topic_error(exc):
                await db.mark_reaction_service_topic_unavailable(channel_id=channel_id)
            await state.clear()
            await message.answer(await render_template(db, channel_id, "reaction.topic_failed"))
            return
        except ValueError:
            await state.clear()
            await message.answer(await render_template(db, channel_id, "reaction.topic_failed"))
            return
        await state.clear()
        settings = await db.get_channel_reaction_settings(channel_id)
        await message.answer(
            await render_template(db, channel_id, result_key, topic=topic_name),
            reply_markup=reaction_settings_keyboard(settings),
        )

    @router.message_reaction()
    async def message_reaction_handler(update: MessageReactionUpdated) -> None:
        await reaction_runtime.handle(update)


    # Legacy group-panel callbacks from the pre-channel panel are kept only as
    # safe tombstones.  They must never bypass the current owner checks or the
    # manual-cleanup preview/confirmation flow.
    @router.callback_query(F.data == "channel:panel_refresh")
    @router.callback_query(F.data == "channel:cleanup_before_yesterday")
    async def legacy_channel_panel_callback(callback: CallbackQuery) -> None:
        await callback.answer(render_default("panel.unavailable", {}), show_alert=True)

    @router.message(ChannelSettingsFlow.anonymous_prefix, F.chat.type == ChatType.PRIVATE)
    async def anonymous_prefix_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id = data.get("settings_channel_id")
        if message.from_user is None or not isinstance(channel_id, int):
            await state.clear()
            await message.answer(render_default("panel.unavailable", {}))
            return
        decision = await authorizer.require(actor_id=message.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS)
        if not decision.allowed:
            await state.clear()
            await message.answer(render_default("panel.unavailable", {}))
            return
        try:
            prefix = await db.set_channel_anonymous_prefix(channel_id=channel_id, prefix=message.text or "")
        except ValueError:
            await message.answer(await render_template(db, channel_id, "settings.anonymous_invalid"))
            return
        await state.clear()
        await message.answer(
            await render_template(db, channel_id, "settings.anonymous_saved", prefix=prefix),
            reply_markup=anonymous_settings_keyboard(),
        )


    # --------------------------------------------------------------
    async def prestart_state_authorized(message: Message, state: FSMContext):
        data = await state.get_data(); channel_id = data.get("prestart_auth_channel_id")
        if message.from_user is None or not isinstance(channel_id, int):
            return False
        decision = await authorizer.require(actor_id=message.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS)
        return decision.allowed

    @router.message(PreStartCardFlow.description)
    async def prestart_description_input(message: Message, state: FSMContext) -> None:
        if not await prestart_state_authorized(message, state):
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        try:
            draft = validate_description(message.text or "")
        except ValueError:
            await message.answer(await render_template(db, int((await state.get_data())["prestart_auth_channel_id"]), "prestart.invalid_text")); return
        await state.update_data(prestart_description_draft=draft)
        await state.set_state(PreStartCardFlow.description_confirmation)
        await send_prestart_preview(message=message, bot=bot, db=db, draft_text=draft)
        await message.answer(await render_template(db, int((await state.get_data())["prestart_auth_channel_id"]), "prestart.text_confirm"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Применить", callback_data="prestart:text:save")],
            [InlineKeyboardButton(text="Отмена", callback_data="prestart:cancel")],
        ]))

    @router.message(PreStartCardFlow.media)
    async def prestart_media_input(message: Message, state: FSMContext) -> None:
        if not await prestart_state_authorized(message, state):
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        media = None
        if message.photo:
            media = ("photo", message.photo[-1].file_id)
        elif message.video:
            media = ("video", message.video.file_id)
        elif message.animation:
            media = ("animation", message.animation.file_id)
        try:
            media_type, media_file_id = validate_media(*(media or (None, None)))
        except ValueError:
            await message.answer(await render_template(db, int((await state.get_data())["prestart_auth_channel_id"]), "prestart.media_prompt")); return
        await state.update_data(prestart_media_type=media_type, prestart_media_file_id=media_file_id)
        await state.set_state(PreStartCardFlow.media_confirmation)
        await send_prestart_preview(message=message, bot=bot, db=db, draft_media=(media_type, media_file_id))
        await message.answer(
            await render_template(db, int((await state.get_data())["prestart_auth_channel_id"]), "prestart.media_confirm"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Сохранить", callback_data="prestart:media:save")],
                [InlineKeyboardButton(text="Отмена", callback_data="prestart:cancel")],
            ]),
        )

    @router.callback_query(F.data.in_({"prestart:text:save", "prestart:media:save", "prestart:cancel"}))
    async def prestart_card_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data(); channel_id = data.get("prestart_auth_channel_id")
        if callback.from_user is None or not isinstance(channel_id, int):
            await state.clear(); await callback.answer(render_default("prestart.stale", {}), show_alert=True); return
        decision = await authorizer.require(actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS)
        if not decision.allowed:
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.data == "prestart:cancel":
            await state.clear(); await callback.answer(await render_template(db, channel_id, "prestart.cancelled")); return
        if callback.data == "prestart:text:save" and await state.get_state() == PreStartCardFlow.description_confirmation.state:
            draft = data.get("prestart_description_draft")
            try:
                normalized = validate_description(draft if isinstance(draft, str) else "")
                await apply_description(bot, normalized)
            except (ValueError, TelegramAPIError):
                await state.clear(); await callback.answer(await render_template(db, channel_id, "prestart.apply_failed"), show_alert=True); return
            await db.set_bot_prestart_description(description=normalized, updated_by=callback.from_user.id)
            await state.clear(); await callback.answer(await render_template(db, channel_id, "prestart.applied"), show_alert=True); return
        if callback.data == "prestart:media:save" and await state.get_state() == PreStartCardFlow.media_confirmation.state:
            try:
                media_type, media_file_id = validate_media(data.get("prestart_media_type"), data.get("prestart_media_file_id"))
            except ValueError:
                await state.clear(); await callback.answer(await render_template(db, channel_id, "prestart.media_stale"), show_alert=True); return
            await db.set_bot_prestart_media(media_type=media_type, media_file_id=media_file_id, updated_by=callback.from_user.id)
            await state.clear()
            await callback.message.answer(
                description_picture_apply_instructions(media_type),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]),
            )
            await callback.answer(await render_template(db, channel_id, "prestart.media_saved")); return
        await state.clear(); await callback.answer(await render_template(db, channel_id, "prestart.stale"), show_alert=True)

    # Channel template editor (private owner panel)
    # --------------------------------------------------------------
    async def template_owner(callback: CallbackQuery):
        return await _panel_callback_channel(callback, authorizer)

    def template_categories_keyboard():
        rows = [[InlineKeyboardButton(text=category, callback_data=f"template:category:{index}:0")] for index, category in enumerate(template_categories())]
        rows.append([InlineKeyboardButton(text="Сбросить все тексты", callback_data="template:reset_all")])
        rows.append([InlineKeyboardButton(text="Назад", callback_data="panel:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data.startswith("template:category:"))
    async def template_category(callback: CallbackQuery) -> None:
        channel = await template_owner(callback)
        if channel is None or callback.message is None: return
        parts=(callback.data or "").split(":")
        try: category=template_categories()[int(parts[2])]; page=max(0,int(parts[3]))
        except (IndexError, ValueError): await callback.answer(ACCESS_DENIED_TEXT,show_alert=True); return
        specs=specs_for_category(category); page=min(page,max(0,(len(specs)-1)//6)); subset=specs[page*6:(page+1)*6]
        overrides=await db.list_template_override_keys(channel_id=int(channel["channel_id"]))
        rows=[[InlineKeyboardButton(text=("✓ " if spec.key in overrides else "")+spec.title,callback_data=f"template:open:{spec.key}")] for spec in subset]
        nav=[]
        if page: nav.append(InlineKeyboardButton(text="◀",callback_data=f"template:category:{parts[2]}:{page-1}"))
        if (page+1)*6<len(specs): nav.append(InlineKeyboardButton(text="▶",callback_data=f"template:category:{parts[2]}:{page+1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton(text="Назад",callback_data="template:home")])
        await callback.message.edit_text(await render_template(db, int(channel["channel_id"]), "template_ui.category_page", category=category, page=page + 1), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()

    @router.callback_query(F.data == "template:home")
    async def template_home(callback: CallbackQuery) -> None:
        channel=await template_owner(callback)
        if channel is None or callback.message is None: return
        await callback.message.edit_text(await render_template(db, int(channel["channel_id"]), "template_ui.home"), reply_markup=template_categories_keyboard()); await callback.answer()

    @router.callback_query(F.data.startswith("template:open:"))
    async def template_open(callback: CallbackQuery) -> None:
        channel=await template_owner(callback)
        if channel is None or callback.message is None:return
        key=(callback.data or "").split(":",2)[-1]; spec=TEMPLATE_REGISTRY.get(key)
        if spec is None: await callback.answer(ACCESS_DENIED_TEXT,show_alert=True); return
        custom=await db.get_template_override(channel_id=int(channel["channel_id"]),template_key=key)
        text=custom if custom is not None else spec.default
        vars=", ".join("{"+item+"}" for item in sorted(spec.variables)) or "нет"
        state="изменён" if custom is not None else "стандартный"
        body=f"<b>{html.escape(spec.title)}</b> ({state})\n{html.escape(spec.description)}\nГде: {html.escape(spec.used_in)}\nАудитория: {html.escape(spec.audience)}\nПеременные: <code>{html.escape(vars)}</code>\n\n<pre>{html.escape(text)}</pre>"
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Изменить",callback_data=f"template:edit:{key}"),InlineKeyboardButton(text="Предпросмотр",callback_data=f"template:preview:{key}")],[InlineKeyboardButton(text="Вернуть стандартный",callback_data=f"template:reset:{key}")],[InlineKeyboardButton(text="Назад",callback_data="template:home")]])
        await callback.message.edit_text(body,reply_markup=kb); await callback.answer()

    @router.callback_query(F.data.startswith("template:preview:"))
    async def template_preview(callback: CallbackQuery) -> None:
        channel=await template_owner(callback)
        if channel is None or callback.message is None:return
        key=(callback.data or "").split(":",2)[-1]; spec=TEMPLATE_REGISTRY.get(key)
        if spec is None: await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        preview=await render_template(db,int(channel["channel_id"]),key,**preview_values(spec))
        await callback.message.answer(f"<b>Предпросмотр</b>\n\n{preview}"); await callback.answer()

    @router.callback_query(F.data.startswith("template:edit:"))
    async def template_edit(callback: CallbackQuery,state:FSMContext)->None:
        channel=await template_owner(callback)
        key=(callback.data or "").split(":",2)[-1]
        if channel is None or key not in TEMPLATE_REGISTRY:return
        await state.clear(); await state.set_state(TemplateFlow.edit); await state.update_data(template_channel_id=int(channel["channel_id"]),template_key=key)
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.edit_prompt")); await callback.answer()

    async def template_state_channel(message:Message,state:FSMContext):
        data=await state.get_data(); cid=data.get("template_channel_id")
        if message.from_user is None or not isinstance(cid,int): return None
        decision=await authorizer.require(actor_id=message.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        return decision.channel if decision.allowed else None

    @router.message(TemplateFlow.edit)
    async def template_edit_text(message:Message,state:FSMContext)->None:
        channel=await template_state_channel(message,state); data=await state.get_data(); key=data.get("template_key"); value=message.text or ""
        if channel is None or not isinstance(key,str): await state.clear(); await message.answer(ACCESS_DENIED_TEXT);return
        try: validate_template(key,value)
        except ValueError: await message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.invalid_text")); return
        await state.update_data(template_draft=value); await state.set_state(TemplateFlow.confirmation)
        await message.answer(f"<b>Предпросмотр</b>\n\n{await render_template_draft(key,value)}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить",callback_data="template:save")],[InlineKeyboardButton(text="Отмена",callback_data="template:cancel")]]))

    async def render_template_draft(key:str,text:str)->str:
        spec=TEMPLATE_REGISTRY[key]; safe={name:html.escape(value) for name,value in preview_values(spec).items()}; return text.format(**safe)

    @router.callback_query(F.data == "template:save")
    async def template_save(callback:CallbackQuery,state:FSMContext)->None:
        data=await state.get_data(); cid,key,draft=data.get("template_channel_id"),data.get("template_key"),data.get("template_draft")
        if callback.from_user is None or not isinstance(cid,int) or not isinstance(key,str) or not isinstance(draft,str) or await state.get_state()!=TemplateFlow.confirmation.state:
            await state.clear(); await callback.answer(render_default("template_ui.stale", {}), show_alert=True);return
        decision=await authorizer.require(actor_id=callback.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        if not decision.allowed: await state.clear();await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        try: validate_template(key,draft)
        except ValueError: await state.clear();await callback.answer(await render_template(db, cid, "template_ui.invalid_saved"), show_alert=True);return
        await db.set_template_override(channel_id=cid,template_key=key,custom_text=draft,updated_by=callback.from_user.id); await state.clear(); await callback.answer(await render_template(db, cid, "template_ui.saved"))

    @router.callback_query(F.data.startswith("template:reset:"))
    async def template_reset_prompt(callback:CallbackQuery,state:FSMContext)->None:
        channel=await template_owner(callback); key=(callback.data or "").split(":",2)[-1]
        if channel is None or key not in TEMPLATE_REGISTRY:return
        await state.clear();await state.set_state(TemplateFlow.reset_one);await state.update_data(template_channel_id=int(channel["channel_id"]),template_key=key)
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.reset_one_prompt"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить",callback_data="template:reset_confirm")],[InlineKeyboardButton(text="Отмена",callback_data="template:cancel")]]));await callback.answer()

    @router.callback_query(F.data == "template:reset_all")
    async def template_reset_all_prompt(callback:CallbackQuery,state:FSMContext)->None:
        channel=await template_owner(callback)
        if channel is None:return
        await state.clear();await state.set_state(TemplateFlow.reset_all);await state.update_data(template_channel_id=int(channel["channel_id"]))
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.reset_all_prompt"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить",callback_data="template:reset_all_confirm")],[InlineKeyboardButton(text="Отмена",callback_data="template:cancel")]]));await callback.answer()

    @router.callback_query(F.data.in_({"template:reset_confirm","template:reset_all_confirm","template:cancel"}))
    async def template_confirm_reset(callback:CallbackQuery,state:FSMContext)->None:
        data=await state.get_data(); cid=data.get("template_channel_id")
        if callback.from_user is None or not isinstance(cid,int):await state.clear();await callback.answer(render_default("template_ui.stale", {}), show_alert=True);return
        if callback.data=="template:cancel": await state.clear();await callback.answer(await render_template(db, cid, "template_ui.cancelled"));return
        decision=await authorizer.require(actor_id=callback.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        if not decision.allowed:await state.clear();await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        if callback.data=="template:reset_confirm" and await state.get_state()==TemplateFlow.reset_one.state:
            key=data.get("template_key");
            if isinstance(key,str): await db.reset_template_override(channel_id=cid,template_key=key)
        elif callback.data=="template:reset_all_confirm" and await state.get_state()==TemplateFlow.reset_all.state:
            await db.reset_all_template_overrides(channel_id=cid)
        else: await state.clear();await callback.answer(await render_template(db, cid, "template_ui.stale"), show_alert=True);return
        await state.clear();await callback.answer(await render_template(db, cid, "template_ui.reset_done"))

    # --------------------------------------------------------------
    # Public /start with channel deep link
    # --------------------------------------------------------------

    @router.message(
        CommandStart(),
        F.chat.type == ChatType.PRIVATE,
    )
    async def start_handler(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        await runtime.remember_user(message.from_user)
        payload = (command.args or "").strip()
        if not payload:
            channels = await db.list_enabled_channels_for_user(message.from_user.id)
            if not channels:
                await message.answer(render_default("channel.no_available", {}))
            elif len(channels) == 1:
                channel = channels[0]
                channel_id = int(channel["channel_id"])
                if await db.set_active_channel(user_id=message.from_user.id, channel_id=channel_id):
                    await message.answer(await render_template(
                        db, channel_id, "channel.selected", channel_name=str(channel["group_title"])
                    ))
                else:
                    await message.answer(render_default("channel.unavailable", {}))
            else:
                await channel_selection_prompt(
                    db, message, channels,
                    current_channel=await trusted_active_channel_for_user(db, message.from_user.id),
                )
            return
        if not payload.startswith("ref_"):
            await message.answer(render_default("setup.deep_link_invalid", {}))
            return
        if payload.startswith("ref_c_"):
            raw = payload[6:]
            channel = await db.get_channel_by_id(int(raw)) if raw.isdigit() else None
        else:
            raw = payload[4:]
            channel = await db.get_legacy_channel_for_owner(int(raw)) if raw.isdigit() else None
        if channel is None or not bool(channel["enabled"]):
            await message.answer(render_default("setup.deep_link_unavailable", {}))
            return
        await db.attach_subscriber(channel_id=int(channel["channel_id"]), user_id=message.from_user.id)
        await message.answer(await render_template(db, int(channel["channel_id"]), "start.greeting", channel_name=str(channel["group_title"])))
        await privacy_handler(message)

    @router.message(Command("channels"), F.chat.type == ChatType.PRIVATE)
    async def channels_handler(message: Message) -> None:
        if not message.from_user:
            return
        channels = await db.list_enabled_channels_for_user(message.from_user.id)
        if not channels:
            await message.answer(render_default("channel.no_available", {}))
            return
        if len(channels) == 1:
            channel = channels[0]
            channel_id = int(channel["channel_id"])
            if not await db.set_active_channel(user_id=message.from_user.id, channel_id=channel_id):
                await message.answer(render_default("channel.unavailable", {}))
                return
            await message.answer(await render_template(
                db, channel_id, "channel.selected", channel_name=str(channel["group_title"])
            ))
            return
        await channel_selection_prompt(
            db, message, channels, current_channel=await trusted_active_channel_for_user(db, message.from_user.id)
        )

    @router.callback_query(F.data.startswith("channel:select:"))
    async def select_channel_handler(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(render_default("channel.unavailable", {}), show_alert=True)
            return
        # set_active_channel verifies both current availability and this user's
        # channel membership; callback data is never treated as authority.
        if not await db.set_active_channel(user_id=callback.from_user.id, channel_id=channel_id):
            await callback.answer(render_default("channel.unavailable", {}), show_alert=True)
            return
        channel = await trusted_active_channel_for_user(db, callback.from_user.id)
        if channel is None or int(channel["channel_id"]) != channel_id:
            await callback.answer(render_default("channel.unavailable", {}), show_alert=True)
            return
        text = await render_template(
            db, channel_id, "channel.selected", channel_name=str(channel["group_title"])
        )
        if callback.message:
            await callback.message.edit_text(text)
        await callback.answer()

    @router.message(Command("privacy"), F.chat.type == ChatType.PRIVATE)
    async def privacy_handler(message: Message) -> None:
        if not message.from_user:
            return
        channel = await trusted_active_channel_for_user(db, message.from_user.id)
        if channel is None:
            await message.answer(render_default("privacy.no_active_channel", {}))
            return
        channel_id = int(channel["channel_id"])
        mode = await db.get_privacy_mode(channel_id=channel_id, user_id=message.from_user.id)
        if mode == "anonymous":
            tag = await db.ensure_anonymous_tag(channel_id=channel_id, user_id=message.from_user.id)
            current = await render_template(
                db, channel_id, "privacy.current_anonymous", anonymous_tag=tag or "Аноним"
            )
        elif mode == "identified":
            current = await render_template(db, channel_id, "privacy.current_identified")
        else:
            current = ""
        prompt = await render_template(db, channel_id, "privacy.prompt")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Анонимно", callback_data="privacy:anonymous"),
            InlineKeyboardButton(text="Открыто", callback_data="privacy:identified"),
        ]])
        text = f"{current}\n\n{prompt}" if current else prompt
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("privacy:"))
    async def privacy_callback(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        mode = (callback.data or "").partition(":")[2]
        channel = await trusted_active_channel_for_user(db, callback.from_user.id)
        if channel is None or mode not in {"anonymous", "identified"}:
            await callback.answer(render_default("privacy.unavailable", {}), show_alert=True)
            return
        channel_id = int(channel["channel_id"])
        current_mode = await db.get_privacy_mode(channel_id=channel_id, user_id=callback.from_user.id)
        if current_mode == mode:
            if mode == "anonymous":
                tag = await db.ensure_anonymous_tag(channel_id=channel_id, user_id=callback.from_user.id)
                text = await render_template(
                    db, channel_id, "privacy.already_anonymous", anonymous_tag=tag or "Аноним"
                )
            else:
                text = await render_template(db, channel_id, "privacy.already_identified")
        else:
            tag = await db.set_privacy_mode(
                channel_id=channel_id, user_id=callback.from_user.id, privacy_mode=mode
            )
            if mode == "anonymous":
                text = await render_template(
                    db, channel_id, "privacy.switched_anonymous", anonymous_tag=tag or "Аноним"
                )
            else:
                text = await render_template(db, channel_id, "privacy.switched_identified")
        if callback.message:
            await callback.message.edit_text(text)
        await callback.answer()

    # --------------------------------------------------------------
    # Subscriber messages
    # --------------------------------------------------------------

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def subscriber_message_handler(message: Message) -> None:
        if not message.from_user:
            return

        channel = await db.get_active_channel_for_user(
            message.from_user.id
        )
        if channel is None:
            await message.answer(render_default("channel.open_personal_link", {}))
            return

        channel_id = int(channel["channel_id"])
        restriction = await db.active_subscriber_restriction_details(
            channel_id=channel_id,
            user_id=message.from_user.id,
        )
        if restriction:
            kind, until, reason, show_reason = restriction
            if kind == "rate_limited" and until is not None:
                state = await db.get_subscriber_moderation(channel_id=channel_id, user_id=message.from_user.id)
                seconds = int(state["rate_limit_seconds"]) if state and state["rate_limit_seconds"] else 0
                if _valid_sanction_duration("rate_limit", seconds):
                    notice_key = (channel_id, message.from_user.id, message.message_id)
                    if notice_key not in restriction_notice_updates:
                        restriction_notice_updates.add(notice_key)
                        if len(restriction_notice_updates) > 2048:
                            restriction_notice_updates.clear()
                        await message.answer(await render_template(
                            db, channel_id, f"sanction.rate.active.{'visible' if show_reason else 'hidden'}",
                            expires_at=until.strftime("%d.%m.%Y %H:%M UTC"),
                            reason=str(reason) if reason else "",
                        ))
            else:
                action_by_kind={"permanently_blocked":"permanent_block","blocked":"temporary_block","muted":"mute"}
                action=action_by_kind[kind]
                duration = _sanction_notice_duration(action, until)
                await message.answer(await render_template(
                    db, channel_id, f"sanction.active.{'visible' if show_reason else 'hidden'}",
                    action=SANCTION_ACTION_LABELS[action], duration=duration,
                    reason=str(reason) if reason else "",
                ))
            return
        privacy_mode = await db.get_privacy_mode(channel_id=channel_id, user_id=message.from_user.id)
        if privacy_mode is None:
            await privacy_handler(message)
            return
        await runtime.accept_user_message(message=message, channel_id=channel_id, group_id=int(channel["group_id"]), privacy_mode=privacy_mode)

    @router.message(Command("subscriber_history"), F.chat.type == ChatType.SUPERGROUP)
    async def subscriber_history_handler(message: Message) -> None:
        if not message.from_user or not message.message_thread_id:
            return
        channel = await db.get_channel_by_group(message.chat.id)
        if channel is None or not (await authorizer.require(actor_id=message.from_user.id, channel_id=int(channel["channel_id"]), action=ChannelAction.SUBSCRIBER, context_group_id=message.chat.id, require_current_telegram_admin=True)).allowed:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        topic = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id)
        if topic is None:
            return
        rows = await db.list_moderation_actions(channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]))
        if not rows:
            await message.answer(await render_template(db, int(topic["channel_id"]), "subscriber.history_empty"))
            return
        lines = [await render_template(db, int(topic["channel_id"]), "subscriber.history_title")]
        for row in rows[:20]:
            reason = f" — {html.escape(str(row['reason']))}" if row["reason"] else ""
            lines.append(f"• <code>{html.escape(str(row['created_at'])[:16])}</code>: {html.escape(str(row['action']))}{reason}")
        await message.answer("\n".join(lines))

    @router.message(Command("subscriber"), F.chat.type == ChatType.SUPERGROUP)
    async def subscriber_handler(message: Message) -> None:
        if not message.message_thread_id:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        topic = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id)
        if topic is None or message.from_user is None:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        decision = await authorizer.require(actor_id=message.from_user.id, channel_id=int(topic["channel_id"]), action=ChannelAction.SUBSCRIBER, context_group_id=message.chat.id, require_current_telegram_admin=True)
        if not decision.allowed:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        state = await db.get_subscriber_moderation(channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]))
        marked = bool(state and state["marked_spam"])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Rate-limit", callback_data=f"subscriber:rate:{int(topic['channel_id'])}:{int(topic['user_id'])}"), InlineKeyboardButton(text="Mute", callback_data=f"subscriber:action:{int(topic['channel_id'])}:{int(topic['user_id'])}:mute")],
            [InlineKeyboardButton(text="\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0430", callback_data=f"subscriber:action:{int(topic['channel_id'])}:{int(topic['user_id'])}:temporary_block"), InlineKeyboardButton(text="\u041f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u0430\u044f \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0430", callback_data=f"subscriber:action:{int(topic['channel_id'])}:{int(topic['user_id'])}:permanent_block")],
            [InlineKeyboardButton(text="\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435", callback_data=f"subscriber:action:{int(topic['channel_id'])}:{int(topic['user_id'])}:warning"), InlineKeyboardButton(text="\u0421\u043d\u044f\u0442\u044c \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u044f", callback_data=f"subscriber:clear:{int(topic['channel_id'])}:{int(topic['user_id'])}")],
            [InlineKeyboardButton(text="\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043c\u0435\u0442\u043a\u0443", callback_data=f"subscriber:meta:add:note:{int(topic['channel_id'])}:{int(topic['user_id'])}"), InlineKeyboardButton(text="\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0442\u0435\u0433", callback_data=f"subscriber:meta:add:tag:{int(topic['channel_id'])}:{int(topic['user_id'])}")],
            [InlineKeyboardButton(text="\u0417\u0430\u043c\u0435\u0442\u043a\u0438", callback_data="subscriber:meta:view:notes:0"), InlineKeyboardButton(text="\u0422\u0435\u0433\u0438", callback_data="subscriber:meta:view:tags:0")],
            [InlineKeyboardButton(text="\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430", callback_data="subscriber:stats")],
            [InlineKeyboardButton(text="\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439", callback_data="subscriber:history:0")],
        ])
        await message.answer(await render_template(db, int(topic["channel_id"]), "subscriber.actions_prompt"), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("subscriber:history:"))
    async def subscriber_restriction_history(callback: CallbackQuery) -> None:
        if callback.message is None: return
        parts=(callback.data or '').split(':')
        detail=len(parts)==5 and parts[2]=='detail'
        try: page=int(parts[4] if detail else parts[2]); item_id=int(parts[3]) if detail else None
        except (IndexError,ValueError): await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        context=await metadata_context(callback.message)
        if context is None or page<0: await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,target=context; cid,uid=int(topic['channel_id']),int(topic['user_id']); channel=await db.get_channel_by_id(cid)
        if channel is None or not bool(channel['enabled']): await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        total=await db.count_subscriber_moderation_history(channel_id=cid,user_id=uid); page=min(page,max(0,(total-1)//8)); rows=await db.get_subscriber_moderation_history(channel_id=cid,user_id=uid,offset=page*8,limit=8)
        labels={'rate_limit':'Rate-limit','mute':'Mute','temporary_block':'Временная блокировка','permanent_block':'Постоянная блокировка','warning':'Предупреждение','clear_restrictions':'Снятие ограничений','mark_spam':'Спам','unmark_spam':'Снятие спама'}
        status={'active':'активно','expired':'истекло','removed':'снято','warning':'выдано','historical':'история'}
        if detail:
            row=next((row for row in rows if int(row['item_id'])==item_id),None)
            if row is None: await callback.answer(await callback_metadata_text(callback, "not_found"),show_alert=True); return
            admin='Администратор' if row.get('admin_id') is not None else 'Системное действие'
            expires=dt_from_db(str(row['expires_at'])).astimezone(ZoneInfo(str(channel['timezone']))).strftime('%d.%m.%Y %H:%M') if row['expires_at'] else '—'
            text=f"<b>{labels.get(str(row['action']),str(row['action']))}</b>\n{target}\nСтатус: {status[str(row['status'])]}\nАдминистратор: {admin}\nПричина: {html.escape(str(row['reason'] or '—'))}\nПричина подписчице: {'да' if row['show_reason_to_subscriber'] else 'нет'}\nИстекает: {expires}"
            await callback.message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Назад',callback_data=f'subscriber:history:{page}')]])); await callback.answer(); return
        zone=ZoneInfo(str(channel['timezone'])); lines=[f"<b>История ограничений</b>\n{target}\nСтраница {page+1} из {max(1,(total+7)//8)}"]
        for row in rows:
            when=dt_from_db(str(row['created_at'])).astimezone(zone).strftime('%d.%m.%Y %H:%M')
            reason=f" ; причина: {html.escape(str(row['reason']))}" if row['reason'] else ''
            lines.append(f"• {labels.get(str(row['action']),str(row['action']))} — {when}; {status[str(row['status'])]}{reason}")
        if not rows: lines.append('Записей пока нет.')
        nav=[]
        event_buttons=[[InlineKeyboardButton(text=f'Подробнее {index+1}',callback_data=f"subscriber:history:detail:{int(row['item_id'])}:{page}")] for index,row in enumerate(rows)]
        if page: nav.append(InlineKeyboardButton(text='◀',callback_data=f'subscriber:history:{page-1}'))
        if (page+1)*8<total: nav.append(InlineKeyboardButton(text='▶',callback_data=f'subscriber:history:{page+1}'))
        await callback.message.answer('\n'.join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=event_buttons+([nav] if nav else [])) if event_buttons or nav else None); await callback.answer()

    @router.callback_query(F.data == "subscriber:stats")
    async def subscriber_statistics(callback: CallbackQuery) -> None:
        if callback.message is None: return
        context=await metadata_context(callback.message)
        if context is None:
            await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,target=context
        channel=await db.get_channel_by_id(int(topic["channel_id"]))
        if channel is None or not bool(channel["enabled"]):
            await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        stats=await db.get_subscriber_statistics(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),timezone_name=str(channel["timezone"]))
        def duration(value): return "—" if value is None else f"{round(float(value)/60,1)} мин"
        media=stats["media"]
        text=(f"<b>Статистика</b>\n{target}\n\n"
              f"Сообщения: <b>{stats['subscriber_messages']}</b>; ответы: <b>{stats['admin_replies']}</b>\n"
              f"Активных дней: {stats['active_days']}; 7д: {stats['last_7_days']}; 30д: {stats['last_30_days']}\n"
              f"Обращения: {stats['conversations']}; отвечено: {stats['answered_conversations']} ({stats['answered_percentage']}%); закрыто: {stats['closed_conversations']}\n"
              f"Среднее сообщений/обращение: {stats['average_messages_per_conversation']}\n"
              f"Первый ответ: среднее {duration(stats['average_first_response_seconds'])}, медиана {duration(stats['median_first_response_seconds'])}\n"
              f"Медиа: текст {media['text']}, фото {media['photo']}, видео {media['video']}, документы {media['document']}, голосовые {media['voice']}, аудио {media['audio']}, стикеры {media['sticker']}, другое {media['other']}\n"
              f"Модерация: предупреждения {stats['moderation']['warnings']}, ограничения {stats['moderation']['restrictions']}, активные {stats['moderation']['active_restrictions']}, заметки {stats['moderation']['notes']}, теги {stats['moderation']['tags']}")
        await callback.message.answer(text); await callback.answer()

    async def channel_ui_text(channel_id: int | None, key: str, **values: object) -> str:
        """Render a channel override when context is valid, else a safe default.

        Stale callbacks intentionally fall back to a default message: no channel
        lookup from callback data means an attacker cannot select another channel's
        custom wording.
        """
        if isinstance(channel_id, int):
            return await render_template(db, channel_id, key, **values)
        return render_default(key, values)

    async def sanction_flow_text(channel_id: int | None, name: str, **values: object) -> str:
        return await channel_ui_text(channel_id, f"sanction.flow.{name}", **values)

    async def metadata_text(channel_id: int | None, name: str, **values: object) -> str:
        return await channel_ui_text(channel_id, f"subscriber.metadata.{name}", **values)

    async def message_ui_text(message: Message, key: str, **values: object) -> str:
        channel = await db.get_channel_by_group(message.chat.id)
        return await channel_ui_text(int(channel["channel_id"]) if channel is not None else None, key, **values)

    async def callback_flow_text(callback: CallbackQuery, name: str, **values: object) -> str:
        if callback.message is None:
            return await sanction_flow_text(None, name, **values)
        return await message_ui_text(callback.message, f"sanction.flow.{name}", **values)

    async def callback_metadata_text(callback: CallbackQuery, name: str, **values: object) -> str:
        if callback.message is None:
            return await metadata_text(None, name, **values)
        return await message_ui_text(callback.message, f"subscriber.metadata.{name}", **values)

    async def metadata_context(message: Message) -> tuple[object, str] | None:
        if message.from_user is None or not message.message_thread_id:
            return None
        topic = await db.get_topic_by_group_thread(
            group_id=message.chat.id, topic_id=message.message_thread_id,
        )
        if topic is None or await authorize_sanction_target(
            message=message, db=db, guard=guard, authorizer=authorizer,
            channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]),
        ) is None:
            return None
        if str(topic["privacy_mode"]) == "anonymous":
            tag = await db.get_anonymous_tag(
                channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]),
            )
            return topic, f"Анонимная подписчица: {tag or 'Анон'}"
        return topic, f"Подписчица #{int(topic['user_id'])}"

    async def metadata_context_from_state(message: Message, state: FSMContext) -> tuple[object, str] | None:
        context = await metadata_context(message)
        data = await state.get_data()
        if context is None:
            return None
        topic, target = context
        if (data.get("channel_id"), data.get("target_user_id")) != (int(topic["channel_id"]), int(topic["user_id"])):
            return None
        return topic, target

    def metadata_cancel_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="subscriber:meta:cancel")
        ]])

    def metadata_page_keyboard(*, kind: str, page: int, total: int, rows: list[object]) -> InlineKeyboardMarkup:
        buttons: list[list[InlineKeyboardButton]] = []
        if kind == "notes":
            buttons.extend([
                [InlineKeyboardButton(text=f"Открыть заметку {index + 1}", callback_data=f"subscriber:meta:note:{int(row['note_id'])}:open:{page}")]
                for index, row in enumerate(rows)
            ])
        else:
            buttons.extend([
                [InlineKeyboardButton(text=f"Удалить тег {index + 1}", callback_data=f"subscriber:meta:tag:{int(row['tag_id'])}:delete:{page}")]
                for index, row in enumerate(rows)
            ])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"subscriber:meta:view:{kind}:{page - 1}"))
        if (page + 1) * (5 if kind == "notes" else 8) < total:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"subscriber:meta:view:{kind}:{page + 1}"))
        if nav:
            buttons.append(nav)
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @router.callback_query(F.data.startswith("subscriber:meta:add:"))
    async def subscriber_metadata_start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None or callback.from_user is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 6 or parts[3] not in {"note", "tag"}:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        try:
            channel_id, user_id = int(parts[4]), int(parts[5])
        except ValueError:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        context = await metadata_context(callback.message)
        if context is None:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True); return
        topic, _ = context
        if (channel_id, user_id) != (int(topic["channel_id"]), int(topic["user_id"])):
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True); return
        await state.clear()
        await state.update_data(channel_id=channel_id, target_user_id=user_id, privacy_mode=str(topic["privacy_mode"]))
        if parts[3] == "note":
            await state.set_state(SubscriberMetadataFlow.note); prompt = await metadata_text(channel_id, "note_prompt")
        else:
            await state.set_state(SubscriberMetadataFlow.tag); prompt = await metadata_text(channel_id, "tag_prompt")
        await callback.message.answer(prompt, reply_markup=metadata_cancel_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("subscriber:meta:view:"))
    async def subscriber_metadata_view(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 5 or parts[3] not in {"notes", "tags"}:
            await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        try: page = int(parts[4])
        except ValueError: page = -1
        if page < 0:
            await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        context = await metadata_context(callback.message)
        if context is None:
            await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True); return
        topic, target = context; channel_id, user_id = int(topic["channel_id"]), int(topic["user_id"])
        kind = parts[3]; limit = 5 if kind == "notes" else 8
        total = await (db.count_subscriber_notes(channel_id=channel_id,user_id=user_id) if kind == "notes" else db.count_subscriber_tags(channel_id=channel_id,user_id=user_id))
        page = min(page, max(0, (total - 1) // limit))
        rows = await (db.list_subscriber_notes(channel_id=channel_id,user_id=user_id,offset=page*limit,limit=limit) if kind == "notes" else db.list_subscriber_tags(channel_id=channel_id,user_id=user_id,offset=page*limit,limit=limit))
        title = await metadata_text(channel_id, f"{kind}_title", target=target, page=page+1, pages=max(1,(total+limit-1)//limit))
        if not rows:
            title += "\n\n" + await metadata_text(channel_id, f"empty_{kind}")
        elif kind == "notes":
            title += "\n\n" + "\n".join(f"{offset + 1}. {html.escape(str(row['note_text'])[:120])}" for offset,row in enumerate(rows))
        else:
            title += "\n\n" + "\n".join(f"• {html.escape(str(row['tag']))}" for row in rows)
        await callback.message.answer(title, reply_markup=metadata_page_keyboard(kind=kind,page=page,total=total,rows=rows))
        await callback.answer()

    @router.callback_query(F.data.startswith("subscriber:meta:note:"))
    async def subscriber_note_action(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 6 or parts[2] != "note" or parts[4] not in {"open", "edit", "delete"}:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        try: note_id, page = int(parts[3]), int(parts[5])
        except ValueError:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True); return
        context = await metadata_context(callback.message)
        if context is None:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True); return
        topic, target = context; channel_id, user_id = int(topic["channel_id"]), int(topic["user_id"])
        note = await db.get_subscriber_note(channel_id=channel_id,user_id=user_id,note_id=note_id)
        if note is None:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "not_found"), show_alert=True); return
        action = parts[4]
        if action == "open":
            rendered = await metadata_text(channel_id, "note_title", target=target, created=str(note["created_at"])[:16], text=str(note["note_text"]))
            keyboard=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Изменить",callback_data=f"subscriber:meta:note:{note_id}:edit:{page}"),InlineKeyboardButton(text="Удалить",callback_data=f"subscriber:meta:note:{note_id}:delete:{page}")],
                [InlineKeyboardButton(text="К заметкам",callback_data=f"subscriber:meta:view:notes:{page}")],
            ])
            await callback.message.answer(rendered,reply_markup=keyboard); await callback.answer(); return
        await state.clear(); await state.update_data(channel_id=channel_id,target_user_id=user_id,privacy_mode=str(topic["privacy_mode"]),note_id=note_id,page=page)
        if action == "edit":
            await state.set_state(SubscriberMetadataFlow.note_edit)
            await callback.message.answer(await callback_metadata_text(callback, "edit_prompt"),reply_markup=metadata_cancel_keyboard())
        else:
            await state.set_state(SubscriberMetadataFlow.note_delete_confirmation)
            await callback.message.answer(await metadata_text(channel_id, "delete_note_confirmation", text=str(note["note_text"])),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Удалить",callback_data="subscriber:meta:notedelete:apply")],
                [InlineKeyboardButton(text="Отмена",callback_data="subscriber:meta:cancel")],
            ]))
        await callback.answer()

    @router.message(SubscriberMetadataFlow.note_edit)
    async def subscriber_note_edit_input(message: Message, state: FSMContext) -> None:
        context = await metadata_context_from_state(message,state)
        value=(message.text or "").strip()
        if context is None:
            await state.clear(); await message.answer(await message_ui_text(message, "subscriber.metadata.access_denied")); return
        if not value or len(" ".join(value.split())) > 1000:
            await message.answer(await message_ui_text(message, "subscriber.metadata.invalid_text")); return
        await state.update_data(pending_note_text=" ".join(value.split()))
        await state.set_state(SubscriberMetadataFlow.note_edit_confirmation)
        channel_id = int(context[0]["channel_id"])
        await message.answer(await metadata_text(channel_id, "edit_confirmation", text=" ".join(value.split())),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сохранить",callback_data="subscriber:meta:noteedit:apply")],
            [InlineKeyboardButton(text="Отмена",callback_data="subscriber:meta:cancel")],
        ]))

    @router.callback_query(F.data == "subscriber:meta:noteedit:apply")
    async def subscriber_note_edit_apply(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        if await state.get_state()!=SubscriberMetadataFlow.note_edit_confirmation.state:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        context=await metadata_context_from_state(callback.message,state); data=await state.get_data()
        if context is None or not isinstance(data.get("note_id"),int) or not isinstance(data.get("pending_note_text"),str):
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,_=context
        try:
            changed=await db.update_subscriber_note(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),note_id=data["note_id"],admin_id=callback.from_user.id,note_text=data["pending_note_text"])
        except ValueError:
            changed=False
        await state.clear()
        await callback.answer(await callback_metadata_text(callback, "note_updated" if changed else "not_found"),show_alert=not changed)

    @router.callback_query(F.data == "subscriber:meta:notedelete:apply")
    async def subscriber_note_delete_apply(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        if await state.get_state()!=SubscriberMetadataFlow.note_delete_confirmation.state:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        context=await metadata_context_from_state(callback.message,state); data=await state.get_data()
        if context is None or not isinstance(data.get("note_id"),int):
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,_=context
        deleted=await db.soft_delete_subscriber_note(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),note_id=data["note_id"],admin_id=callback.from_user.id)
        await state.clear(); await callback.answer(await callback_metadata_text(callback, "note_deleted" if deleted else "not_found"),show_alert=not deleted)

    @router.callback_query(F.data.startswith("subscriber:meta:tag:"))
    async def subscriber_tag_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        parts=(callback.data or "").split(":")
        if len(parts)!=6 or parts[2]!="tag" or parts[4]!="delete":
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        try: tag_id,page=int(parts[3]),int(parts[5])
        except ValueError:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        context=await metadata_context(callback.message)
        if context is None:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,_=context; tag=await db.get_subscriber_tag(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),tag_id=tag_id)
        if tag is None:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "not_found"),show_alert=True); return
        await state.clear(); await state.update_data(channel_id=int(topic["channel_id"]),target_user_id=int(topic["user_id"]),privacy_mode=str(topic["privacy_mode"]),tag_id=tag_id,page=page)
        await state.set_state(SubscriberMetadataFlow.tag_delete_confirmation)
        await callback.message.answer(await metadata_text(int(topic["channel_id"]), "delete_tag_confirmation", tag=str(tag["tag"])),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Удалить",callback_data="subscriber:meta:tagdelete:apply")],
            [InlineKeyboardButton(text="Отмена",callback_data="subscriber:meta:cancel")],
        ])); await callback.answer()

    @router.callback_query(F.data == "subscriber:meta:tagdelete:apply")
    async def subscriber_tag_delete_apply(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None:
            return
        if await state.get_state()!=SubscriberMetadataFlow.tag_delete_confirmation.state:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        context=await metadata_context_from_state(callback.message,state); data=await state.get_data()
        if context is None or not isinstance(data.get("tag_id"),int):
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "access_denied"),show_alert=True); return
        topic,_=context
        deleted=await db.delete_subscriber_tag(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),tag_id=data["tag_id"],admin_id=callback.from_user.id)
        await state.clear(); await callback.answer(await callback_metadata_text(callback, "tag_deleted" if deleted else "not_found"),show_alert=not deleted)

    @router.callback_query(F.data == "subscriber:meta:cancel")
    async def subscriber_metadata_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        valid={state_item.state for state_item in (SubscriberMetadataFlow.note,SubscriberMetadataFlow.tag,SubscriberMetadataFlow.note_edit,SubscriberMetadataFlow.note_edit_confirmation,SubscriberMetadataFlow.note_delete_confirmation,SubscriberMetadataFlow.tag_delete_confirmation)}
        if await state.get_state() not in valid:
            await state.clear(); await callback.answer(await callback_metadata_text(callback, "expired"),show_alert=True); return
        await state.clear(); await callback.answer(await callback_metadata_text(callback, "cancelled"))

    @router.message(SubscriberMetadataFlow.note)
    async def subscriber_note_input(message: Message, state: FSMContext) -> None:
        context=await metadata_context_from_state(message,state)
        if context is None:
            await state.clear(); await message.answer(await message_ui_text(message, "subscriber.metadata.access_denied")); return
        topic,_=context
        try: await db.add_subscriber_note(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),admin_id=message.from_user.id,note_text=message.text or "")
        except ValueError:
            await message.answer(await message_ui_text(message, "subscriber.metadata.invalid_text")); return
        await state.clear(); await message.answer(await message_ui_text(message, "subscriber.metadata.saved_note"))

    @router.message(SubscriberMetadataFlow.tag)
    async def subscriber_tag_input(message: Message, state: FSMContext) -> None:
        context=await metadata_context_from_state(message,state)
        if context is None:
            await state.clear(); await message.answer(await message_ui_text(message, "subscriber.metadata.access_denied")); return
        topic,_=context
        try: created=await db.add_subscriber_tag(channel_id=int(topic["channel_id"]),user_id=int(topic["user_id"]),admin_id=message.from_user.id,tag=message.text or "")
        except ValueError:
            await message.answer(await message_ui_text(message, "subscriber.metadata.invalid_text")); return
        await state.clear(); await message.answer(await message_ui_text(message, "subscriber.metadata.saved_tag" if created else "subscriber.metadata.duplicate_tag"))

    @router.callback_query(F.data.startswith("subscriber:action:"))
    async def subscriber_sanction_action(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None or callback.from_user is None: return
        parts=(callback.data or "").split(":")
        if len(parts)!=5:
            await state.clear(); await callback.answer(await callback_flow_text(callback, "invalid_callback"),show_alert=True); return
        try: channel_id,user_id=int(parts[2]),int(parts[3])
        except ValueError:
            await state.clear(); await callback.answer(await callback_flow_text(callback, "invalid_callback"),show_alert=True); return
        action=parts[4]
        if action not in {"mute","temporary_block","permanent_block","warning"} or await authorize_sanction_target(message=callback.message,db=db,guard=guard,authorizer=authorizer,channel_id=channel_id,user_id=user_id) is None:
            await state.clear(); await callback.answer(await callback_flow_text(callback, "access_denied"),show_alert=True); return
        topic=await db.get_topic_by_group_thread(group_id=callback.message.chat.id,topic_id=callback.message.message_thread_id or 0)
        if topic is None:
            await state.clear(); await callback.answer(await callback_flow_text(callback, "expired"),show_alert=True); return
        await state.clear(); await state.update_data(channel_id=channel_id,target_user_id=user_id,privacy_mode=str(topic["privacy_mode"]),sanction_type=action,sanction_parameters={},reason_choice=None,custom_reason=None,show_reason_to_subscriber=None)
        if action in {"mute","temporary_block"}:
            await state.set_state(SanctionFlow.parameters)
            await callback.message.answer(
                await callback_flow_text(callback, "choose_duration"),
                reply_markup=sanction_duration_keyboard(action),
            )
        else:
            await state.set_state(SanctionFlow.reason)
            await callback.message.answer(await callback_flow_text(callback, "choose_reason"),reply_markup=sanction_reason_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("subscriber:rate:"))
    async def subscriber_rate_menu(callback: CallbackQuery, state: FSMContext) -> None:
        """Start a sanction request; it must never mutate moderation state."""
        if callback.message is None or callback.from_user is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        try:
            channel_id, user_id = int(parts[2]), int(parts[3])
        except ValueError:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        channel = await authorize_sanction_target(
            message=callback.message,
            db=db,
            guard=guard,
            authorizer=authorizer,
            channel_id=channel_id,
            user_id=user_id,
        )
        if channel is None:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "access_denied"), show_alert=True)
            return
        context_topic = await db.get_topic_by_group_thread(
            group_id=callback.message.chat.id,
            topic_id=callback.message.message_thread_id or 0,
        )
        if context_topic is None:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        # Do not put profile fields in FSM. The topic's privacy mode is enough
        # for future screens to choose an anonymous-safe presentation.
        await state.clear()
        await state.set_state(SanctionFlow.parameters)
        await state.update_data(
            channel_id=channel_id,
            target_user_id=user_id,
            privacy_mode=str(context_topic["privacy_mode"]),
            sanction_type="rate_limit",
            sanction_parameters={},
            reason_choice=None,
            custom_reason=None,
            show_reason_to_subscriber=None,
        )
        keyboard = sanction_duration_keyboard("rate_limit")
        await callback.message.answer(await callback_flow_text(callback, "choose_duration"), reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data == "sanction:cancel")
    async def sanction_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer(await callback_flow_text(callback, "cancelled"))

    @router.callback_query(F.data.startswith("sanction:param:"))
    async def sanction_duration_parameter(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if await state.get_state() != SanctionFlow.parameters.state or len(parts) != 4:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        action = parts[2]
        if data.get("sanction_type") != action or action not in {"rate_limit", "mute", "temporary_block"}:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        if parts[3] == "custom":
            await state.update_data(awaiting_custom_duration=True)
            if callback.message is not None:
                await callback.message.answer(await callback_flow_text(callback, "custom_duration"))
            await callback.answer()
            return
        try:
            seconds = int(parts[3])
        except ValueError:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        allowed = RATE_LIMIT_SECONDS if action == "rate_limit" else DURATION_SECONDS
        if seconds not in allowed:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        parameters = {"rate_limit_seconds": seconds} if action == "rate_limit" else {"duration_seconds": seconds}
        await state.update_data(sanction_parameters=parameters, awaiting_custom_duration=False)
        await state.set_state(SanctionFlow.reason)
        if callback.message is not None:
            await callback.message.answer(
                await callback_flow_text(callback, "choose_reason"),
                reply_markup=sanction_reason_keyboard(),
            )
        await callback.answer()

    @router.message(SanctionFlow.parameters)
    async def sanction_custom_duration(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        action = data.get("sanction_type")
        if not data.get("awaiting_custom_duration") or action not in {"rate_limit", "mute", "temporary_block"}:
            await state.clear()
            await message.answer(await message_ui_text(message, "sanction.flow.expired"))
            return
        try:
            minutes = int((message.text or "").strip())
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= 10080:
            await message.answer(await message_ui_text(message, "sanction.flow.invalid_duration"))
            return
        seconds = minutes * 60
        parameters = {"rate_limit_seconds": seconds} if action == "rate_limit" else {"duration_seconds": seconds}
        await state.update_data(sanction_parameters=parameters, awaiting_custom_duration=False)
        await state.set_state(SanctionFlow.reason)
        await message.answer(
            await message_ui_text(message, "sanction.flow.choose_reason"),
            reply_markup=sanction_reason_keyboard(),
        )

    @router.callback_query(F.data.startswith("sanction:reason:"))
    async def sanction_reason_choice(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if await state.get_state() != SanctionFlow.reason.state or len(parts) != 3:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        choice = parts[2]
        if choice not in SANCTION_REASON_CHOICES or not isinstance(data.get("channel_id"), int):
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        if choice == "other":
            await state.update_data(reason_choice="other", custom_reason=None)
            await state.set_state(SanctionFlow.custom_reason)
            if callback.message is not None:
                await callback.message.answer(await callback_flow_text(callback, "custom_reason"))
            await callback.answer()
            return
        await state.update_data(reason_choice=choice, custom_reason=None)
        await state.set_state(SanctionFlow.visibility)
        if callback.message is not None:
            await callback.message.answer(
                await callback_flow_text(callback, "choose_visibility"),
                reply_markup=sanction_visibility_keyboard(),
            )
        await callback.answer()

    @router.message(SanctionFlow.custom_reason)
    async def sanction_custom_reason(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        value = (message.text or "").strip()
        if data.get("reason_choice") != "other" or not isinstance(data.get("channel_id"), int):
            await state.clear()
            await message.answer(await message_ui_text(message, "sanction.flow.expired"))
            return
        if not value:
            await message.answer(await message_ui_text(message, "sanction.flow.invalid_reason"))
            return
        await state.update_data(custom_reason=value[:1000])
        await state.set_state(SanctionFlow.visibility)
        await message.answer(
            await message_ui_text(message, "sanction.flow.choose_visibility"),
            reply_markup=sanction_visibility_keyboard(),
        )

    @router.callback_query(F.data.startswith("sanction:visibility:"))
    async def sanction_visibility_choice(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if await state.get_state() != SanctionFlow.visibility.state or len(parts) != 3:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        visible_values = {"yes": True, "no": False}
        if parts[2] not in visible_values:
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        await state.update_data(show_reason_to_subscriber=visible_values[parts[2]])
        data = await state.get_data()
        if not sanction_flow_is_complete(data):
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        anonymous_tag = None
        if data["privacy_mode"] == "anonymous":
            anonymous_tag = await db.get_anonymous_tag(
                channel_id=int(data["channel_id"]),
                user_id=int(data["target_user_id"]),
            )
        await state.set_state(SanctionFlow.confirmation)
        if callback.message is not None:
            await callback.message.answer(
                sanction_confirmation_text(data, anonymous_tag=anonymous_tag),
                reply_markup=sanction_confirmation_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "sanction:apply")
    async def sanction_apply(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if await state.get_state() != SanctionFlow.confirmation.state or not sanction_flow_is_complete(data):
            await state.clear()
            await callback.answer(await callback_flow_text(callback, "expired"), show_alert=True)
            return
        # Move away from confirmation before awaiting the DB call.  A second
        # delivery of the same Telegram callback then cannot create a second
        # journal record.
        await state.set_state(SanctionFlow.action)
        try:
            reason = await apply_sanction_from_flow(
                message=callback.message,
                db=db,
                guard=guard,
                authorizer=authorizer,
                flow_data=data,
            ) if callback.message is not None else None
        except Exception:
            logger.exception("Sanction finalisation failed")
            reason = None
        await state.clear()
        if reason is None:
            await callback.answer(await callback_flow_text(callback, "apply_failed"), show_alert=True)
            return
        action=str(data["sanction_type"])
        parameters=data["sanction_parameters"]
        seconds=_sanction_duration_seconds(action, parameters)
        until=utc_now()+timedelta(seconds=int(seconds)) if isinstance(seconds,int) and action != "rate_limit" else None
        if action == "rate_limit":
            moderation=await db.get_subscriber_moderation(channel_id=int(data["channel_id"]),user_id=int(data["target_user_id"]))
            stored_reason=str(moderation["sanction_reason"]) if moderation and moderation["sanction_reason"] else None
            stored_visibility=bool(moderation and moderation["show_reason_to_subscriber"])
            delivered=await deliver_rate_limit_notification(bot=bot,db=db,channel_id=int(data["channel_id"]),user_id=int(data["target_user_id"]),seconds=int(seconds),until=None,reason=stored_reason,show_reason=stored_visibility)
        else:
            stored_reason=reason; stored_visibility=bool(data["show_reason_to_subscriber"])
            delivered=await deliver_sanction_notification(bot=bot,db=db,channel_id=int(data["channel_id"]),user_id=int(data["target_user_id"]),action=action,until=until,reason=stored_reason,show_reason=stored_visibility)
        if callback.message is not None:
            tag = None
            if data["privacy_mode"] == "anonymous":
                tag = await db.get_anonymous_tag(
                    channel_id=int(data["channel_id"]),
                    user_id=int(data["target_user_id"]),
                )
            delivery_status = await callback_flow_text(callback, "delivery_sent" if delivered else "delivery_failed")
            confirmation = await render_template(
                db,
                int(data["channel_id"]),
                "sanction.flow.confirmation",
                **sanction_confirmation_values(data, anonymous_tag=tag),
            )
            await callback.message.answer(f"{confirmation}\n\n{delivery_status}")
        await callback.answer()

    @router.message(SanctionFlow.reason)
    @router.message(SanctionFlow.visibility)
    @router.message(SanctionFlow.confirmation)
    async def sanction_callback_expected(message: Message) -> None:
        await message.answer(await message_ui_text(message, "sanction.flow.callback_expected"))

    # Telegram can deliver buttons from old cards after a release.  Explicitly
    # reject the former immediate-action callback so it cannot bypass the FSM.
    @router.callback_query(F.data.startswith("subscriber:set_rate:"))
    async def legacy_subscriber_set_rate(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)

    @router.callback_query(F.data.startswith("subscriber:clear:"))
    async def subscriber_clear_restrictions(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.message is None or callback.from_user is None:return
        parts=(callback.data or "").split(":")
        try: channel_id,user_id=int(parts[2]),int(parts[3])
        except (IndexError,ValueError): await callback.answer(await callback_flow_text(callback, "invalid_callback"),show_alert=True);return
        if await authorize_sanction_target(message=callback.message,db=db,guard=guard,authorizer=authorizer,channel_id=channel_id,user_id=user_id) is None:
            await state.clear();await callback.answer(await callback_flow_text(callback, "access_denied"),show_alert=True);return
        active = await db.list_active_sanctions(channel_id=channel_id, user_id=user_id)
        if not active:
            await callback.answer(await callback_flow_text(callback, "no_active"), show_alert=True)
            return
        await state.clear()
        await state.set_state(SanctionFlow.action)
        await state.update_data(channel_id=channel_id, target_user_id=user_id, clear_restrictions=True)
        labels = ", ".join(SANCTION_ACTION_LABELS[str(row["action"])] for row in active)
        await callback.message.answer(
            await callback_flow_text(callback, "clear_confirmation", actions=labels),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить", callback_data="sanction:clear_confirm")],
                [InlineKeyboardButton(text="Отмена", callback_data="sanction:cancel")],
            ]),
        )
        await callback.answer()


    @router.callback_query(F.data == "sanction:clear_confirm")
    async def subscriber_clear_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data=await state.get_data()
        if callback.message is None or await state.get_state()!=SanctionFlow.action.state or not data.get("clear_restrictions"):
            await state.clear();await callback.answer(await callback_flow_text(callback, "expired"),show_alert=True);return
        channel_id,user_id=data.get("channel_id"),data.get("target_user_id")
        if not isinstance(channel_id,int) or not isinstance(user_id,int) or await authorize_sanction_target(message=callback.message,db=db,guard=guard,authorizer=authorizer,channel_id=channel_id,user_id=user_id) is None:
            await state.clear();await callback.answer(await callback_flow_text(callback, "access_denied"),show_alert=True);return
        count = await db.revoke_active_sanctions(
            channel_id=channel_id, user_id=user_id, admin_id=callback.from_user.id
        )
        await state.clear()
        await callback.answer(await callback_flow_text(callback, "cleared", count=count))

    @router.callback_query(F.data.startswith("subscriber:spam:"))
    async def subscriber_spam_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        try:
            _, _, channel_raw, user_raw = (callback.data or "").split(":")
            channel_id, user_id = int(channel_raw), int(user_raw)
        except (ValueError, IndexError):
            await callback.answer(await callback_flow_text(callback, "invalid_callback"), show_alert=True)
            return
        topic = await db.get_topic_by_group_thread(group_id=callback.message.chat.id, topic_id=callback.message.message_thread_id or 0)
        decision = await authorizer.require(actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.MODERATION, context_group_id=callback.message.chat.id, require_current_telegram_admin=True)
        if not decision.allowed or topic is None or int(topic["channel_id"]) != channel_id or int(topic["user_id"]) != user_id:
            await callback.answer(await render_template(db, channel_id, "subscriber.spam_unavailable"), show_alert=True)
            return
        state = await db.get_subscriber_moderation(channel_id=int(channel_raw), user_id=int(user_raw))
        marked = not bool(state and state["marked_spam"])
        await db.update_subscriber_moderation(channel_id=int(channel_raw), user_id=int(user_raw), marked_spam=marked)
        await db.record_moderation_action(channel_id=int(channel_raw), user_id=int(user_raw), admin_id=callback.from_user.id, action="mark_spam" if marked else "unmark_spam")
        await callback.answer(await render_template(db, channel_id, "subscriber.spam_updated"))

    status_labels = {"new": "Новый", "in_progress": "В работе", "answered": "Отвечено", "closed": "Закрыто"}

    def topic_status_keyboard(topic) -> InlineKeyboardMarkup:
        current = str(topic["status"])
        rows = [
            [
                InlineKeyboardButton(text=("✓ " if current == "new" else "") + "Новое", callback_data="topic:status:new"),
                InlineKeyboardButton(text=("✓ " if current == "in_progress" else "") + "В работе", callback_data="topic:status:in_progress"),
            ],
            [
                InlineKeyboardButton(text=("✓ " if current == "answered" else "") + "Отвечено", callback_data="topic:status:answered"),
                InlineKeyboardButton(text=("✓ " if current == "closed" else "") + "Закрыто", callback_data="topic:status:closed"),
            ],
            [
                InlineKeyboardButton(
                    text="Снять важность" if bool(topic["is_important"]) else "Отметить важной",
                    callback_data="topic:protect:important:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Снять защиту от очистки" if bool(topic["is_pinned"]) else "Защитить от очистки",
                    callback_data="topic:protect:pinned:toggle",
                )
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def topic_status_text(topic) -> str:
        return await render_template(
            db,
            int(topic["channel_id"]),
            "status.overview",
            status=status_labels.get(str(topic["status"]), str(topic["status"])),
            important="Да" if bool(topic["is_important"]) else "Нет",
            pinned="Да" if bool(topic["is_pinned"]) else "Нет",
        )

    @router.message(Command("status"), F.chat.type == ChatType.SUPERGROUP)
    async def status_handler(message: Message, command: CommandObject) -> None:
        if not message.message_thread_id:
            await message.answer(render_default("status.context_required", {}))
            return
        topic = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id)
        if topic is None:
            await message.answer(render_default("status.unavailable", {}))
            return
        if message.from_user is None or not (await authorizer.require(actor_id=message.from_user.id, channel_id=int(topic["channel_id"]), action=ChannelAction.SUBSCRIBER, context_group_id=message.chat.id, require_current_telegram_admin=True)).allowed:
            await message.answer(await render_template(db, int(topic["channel_id"]), "access.denied"))
            return
        value = (command.args or "").strip().lower()
        if not value:
            await message.answer(await topic_status_text(topic), reply_markup=topic_status_keyboard(topic))
            return
        aliases = {"new": "new", "новый": "new", "новое": "new", "in_progress": "in_progress", "в_работе": "in_progress", "answered": "answered", "отвечен": "answered", "отвечено": "answered", "closed": "closed", "закрыт": "closed", "закрыто": "closed"}
        status = aliases.get(value)
        if status is None:
            await message.answer(await render_template(db, int(topic["channel_id"]), "status.usage"), reply_markup=topic_status_keyboard(topic))
            return
        await db.set_topic_status(channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]), privacy_mode=str(topic["privacy_mode"]), status=status)
        updated = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id)
        await message.answer(
            await render_template(db, int(topic["channel_id"]), "status.changed", status=status_labels[status]),
            reply_markup=topic_status_keyboard(updated or topic),
        )

    @router.callback_query(F.data.startswith("topic:status:"))
    async def topic_status_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.message.message_thread_id:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        status = (callback.data or "").rsplit(":", 1)[-1]
        if status not in status_labels:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        topic = await db.get_topic_by_group_thread(group_id=callback.message.chat.id, topic_id=callback.message.message_thread_id)
        if topic is None:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=int(topic["channel_id"]),
            action=ChannelAction.SUBSCRIBER, context_group_id=callback.message.chat.id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await callback.answer(await render_template(db, int(topic["channel_id"]), "access.denied"), show_alert=True)
            return
        await db.set_topic_status(channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]), privacy_mode=str(topic["privacy_mode"]), status=status)
        updated = await db.get_topic_by_group_thread(group_id=callback.message.chat.id, topic_id=callback.message.message_thread_id)
        if updated is None:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        await callback.message.edit_text(await topic_status_text(updated), reply_markup=topic_status_keyboard(updated))
        await callback.answer(await render_template(db, int(topic["channel_id"]), "status.changed", status=status_labels[status]))

    @router.callback_query(F.data.startswith("topic:protect:"))
    async def topic_protection_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.message.message_thread_id:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4 or parts[2] not in {"important", "pinned"} or parts[3] != "toggle":
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        topic = await db.get_topic_by_group_thread(group_id=callback.message.chat.id, topic_id=callback.message.message_thread_id)
        if topic is None:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=int(topic["channel_id"]),
            action=ChannelAction.SUBSCRIBER, context_group_id=callback.message.chat.id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await callback.answer(await render_template(db, int(topic["channel_id"]), "access.denied"), show_alert=True)
            return
        field = parts[2]
        value = not bool(topic["is_important"] if field == "important" else topic["is_pinned"])
        kwargs = {field: value}
        await db.set_topic_cleanup_protection(
            channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]),
            privacy_mode=str(topic["privacy_mode"]), **kwargs,
        )
        updated = await db.get_topic_by_group_thread(group_id=callback.message.chat.id, topic_id=callback.message.message_thread_id)
        if updated is None:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        await callback.message.edit_text(await topic_status_text(updated), reply_markup=topic_status_keyboard(updated))
        await callback.answer(await render_template(db, int(topic["channel_id"]), "status.protection_changed"))

    # --------------------------------------------------------------
    # Channel-wide mass broadcast from General (owner only)
    # --------------------------------------------------------------

    async def _broadcast_callback_context(callback: CallbackQuery, broadcast_id: str):
        if callback.message is None or callback.from_user is None:
            return None, None
        message = callback.message
        if message.chat.type != ChatType.SUPERGROUP or not is_general_forum_message(message):
            await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
            return None, None
        channel = await db.get_channel_by_group(message.chat.id)
        if channel is None:
            await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
            return None, None
        channel_id = int(channel["channel_id"])
        decision = await authorizer.require(
            actor_id=callback.from_user.id,
            channel_id=channel_id,
            action=ChannelAction.BROADCAST,
            context_group_id=message.chat.id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await callback.answer(render_default("broadcast.owner_required", {}), show_alert=True)
            return None, None
        broadcast = await db.get_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
        if broadcast is None or int(broadcast["created_by"]) != callback.from_user.id:
            await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
            return None, None
        return decision.channel, broadcast

    @router.message(Command("broadcast"))
    async def broadcast_start_handler(message: Message, state: FSMContext) -> None:
        if message.chat.type != ChatType.SUPERGROUP or not is_general_forum_message(message):
            await message.answer(render_default("broadcast.general_required", {}))
            return
        channel = await db.get_channel_by_group(message.chat.id)
        if channel is None or message.from_user is None:
            await message.answer(render_default("broadcast.general_required", {}))
            return
        decision = await authorizer.require(
            actor_id=message.from_user.id,
            channel_id=int(channel["channel_id"]),
            action=ChannelAction.BROADCAST,
            context_group_id=message.chat.id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await message.answer(render_default("broadcast.owner_required", {}))
            return
        channel_id = int(channel["channel_id"])
        active = await db.get_sending_broadcast(channel_id=channel_id)
        await state.clear()
        if active is not None:
            broadcast_id = str(active["broadcast_id"])
            await message.answer(
                await render_template(db, channel_id, "broadcast.resume_available"),
                reply_markup=broadcast_resume_keyboard(broadcast_id),
            )
            return
        await state.set_state(BroadcastFlow.message)
        await state.update_data(channel_id=channel_id, group_id=message.chat.id, owner_id=message.from_user.id)
        await message.answer(await render_template(db, channel_id, "broadcast.prompt"))

    @router.message(BroadcastFlow.message, F.chat.type == ChatType.SUPERGROUP)
    async def broadcast_capture_handler(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id = data.get("channel_id")
        group_id = data.get("group_id")
        owner_id = data.get("owner_id")
        if not all(isinstance(value, int) for value in (channel_id, group_id, owner_id)):
            await state.clear()
            await message.answer(render_default("broadcast.unavailable", {}))
            return
        if message.chat.id != group_id or not is_general_forum_message(message):
            await message.answer(render_default("broadcast.general_required", {}))
            return
        if message.from_user is None or message.from_user.id != owner_id:
            await state.clear()
            await message.answer(render_default("broadcast.owner_required", {}))
            return
        decision = await authorizer.require(
            actor_id=message.from_user.id,
            channel_id=channel_id,
            action=ChannelAction.BROADCAST,
            context_group_id=message.chat.id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await state.clear()
            await message.answer(render_default("broadcast.owner_required", {}))
            return
        if not broadcast_message_is_copyable(message):
            await message.answer(await render_template(db, channel_id, "broadcast.unsupported"))
            return
        if message.media_group_id:
            await broadcast_albums.push(BroadcastAlbumItem(
                message=message, state=state, channel_id=channel_id, group_id=group_id, owner_id=owner_id,
            ))
            return

        # copy_message is both the preview and a capability check for this exact
        # source message type. The source is persisted only after preview succeeds.
        try:
            await bot.copy_message(
                chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=message.message_id,
            )
        except TelegramAPIError:
            await message.answer(await render_template(db, channel_id, "broadcast.unsupported"))
            return

        broadcast_id = await _persist_broadcast_source(
            state=state, channel_id=channel_id, group_id=group_id, owner_id=owner_id,
            message_ids=[message.message_id], media_group_id=None,
        )
        if broadcast_id is None:
            await state.clear()
            await message.answer(render_default("broadcast.unavailable", {}))
            return
        await message.answer(
            await render_template(db, channel_id, "broadcast.preview_ready"),
            reply_markup=broadcast_preview_keyboard(broadcast_id),
        )

    @router.callback_query(F.data.startswith("broadcast:"))
    async def broadcast_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":", 2)
        if len(parts) != 3 or parts[1] not in {"send", "edit", "cancel", "resume"} or not parts[2]:
            await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
            return
        action, broadcast_id = parts[1], parts[2]
        channel, broadcast = await _broadcast_callback_context(callback, broadcast_id)
        if channel is None or broadcast is None or callback.message is None or callback.from_user is None:
            return
        channel_id = int(channel["channel_id"])
        status = str(broadcast["status"])

        if action == "edit":
            if status != "draft":
                await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
                return
            await state.clear()
            await state.set_state(BroadcastFlow.message)
            await state.update_data(
                channel_id=channel_id,
                group_id=callback.message.chat.id,
                owner_id=callback.from_user.id,
                broadcast_id=broadcast_id,
            )
            await callback.message.answer(await render_template(db, channel_id, "broadcast.prompt"))
            await callback.answer()
            return

        if action == "cancel":
            if status != "draft" or not await db.cancel_broadcast_draft(
                broadcast_id=broadcast_id,
                channel_id=channel_id,
                created_by=callback.from_user.id,
            ):
                await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
                return
            await state.clear()
            await callback.message.edit_text(await render_template(db, channel_id, "broadcast.cancelled"))
            await callback.answer()
            return

        if action == "send":
            if status != "draft":
                await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
                return
            claimed = await db.claim_broadcast_for_send(
                broadcast_id=broadcast_id,
                channel_id=channel_id,
                created_by=callback.from_user.id,
            )
            if not claimed:
                active = await db.get_sending_broadcast(channel_id=channel_id)
                text = "broadcast.conflict" if active is not None else "broadcast.unavailable"
                await callback.answer(
                    await render_template(db, channel_id, text) if active is not None else render_default(text, {}),
                    show_alert=True,
                )
                return
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            await callback.message.answer(await render_template(db, channel_id, "broadcast.started"))
        else:  # resume
            if status != "sending":
                await callback.answer(render_default("broadcast.unavailable", {}), show_alert=True)
                return
            await state.clear()
            await callback.answer()
            await callback.message.answer(await render_template(db, channel_id, "broadcast.started"))

        summary = await broadcast_runtime.deliver(broadcast_id=broadcast_id, channel_id=channel_id)
        await callback.message.answer(await render_template(
            db,
            channel_id,
            "broadcast.summary",
            unique_recipients=summary.unique_recipients,
            delivered=summary.delivered,
            undelivered=summary.undelivered,
            skipped=summary.skipped,
            errors=summary.errors,
        ))

    # --------------------------------------------------------------
    # Messages from channel admin groups to subscribers
    # --------------------------------------------------------------

    @router.message(F.chat.type == ChatType.SUPERGROUP)
    async def admin_group_message_handler(message: Message) -> None:
        channel = await db.get_channel_by_group(message.chat.id)
        if channel is None:
            return

        # Сервисные сообщения о темах пользователю не отправляем.
        if (
            message.forum_topic_created
            or message.forum_topic_closed
            or message.forum_topic_reopened
            or message.forum_topic_edited
        ):
            return

        if not message.message_thread_id:
            return

        if message_is_admin_command(message):
            return

        # Сообщения самого бота (карточка пользователя, ошибки доставки,
        # панель и т.п.) нельзя отправлять обратно подписчику.
        if (
            message.from_user
            and message.from_user.is_bot
            and not (
                message.sender_chat
                and message.sender_chat.id == message.chat.id
            )
        ):
            return

        topic = await db.get_topic_by_group_thread(
            group_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        if topic is None:
            return

        if message.from_user is None or not (await authorizer.require(actor_id=message.from_user.id, channel_id=int(topic["channel_id"]), action=ChannelAction.ADMIN_REPLY, context_group_id=message.chat.id, require_current_telegram_admin=True)).allowed:
            return

        await runtime.accept_admin_message(
            message=message,
            channel_id=int(topic["channel_id"]),
            user_id=int(topic["user_id"]),
            group_id=message.chat.id,
            topic_id=int(topic["topic_id"]),
            privacy_mode=str(topic["privacy_mode"]),
        )

    dispatcher.include_router(router)
