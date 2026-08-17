import asyncio
import html
import io
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

from authorization import ChannelAction, ChannelAuthorizer, GlobalAction, GlobalAuthorizer
from broadcast_runtime import BroadcastRuntime
from reaction_runtime import ReactionRuntime
from command_menu import sync_command_menus
from custom_transfer import (
    CUSTOM_PACK_SCHEMA_VERSION,
    MAX_CUSTOM_PACK_BYTES,
    CustomPackValidationError,
    dumps_export_document,
    normalize_import_document,
    parse_import_bytes,
)
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
from templates import (
    TEMPLATE_REGISTRY,
    TemplateValidationError,
    channel_categories as template_categories,
    friendly_placeholder,
    normalize_editor_template,
    preview_values,
    render_default,
    render_label,
    render_template,
    channel_specs_for_category as specs_for_category,
    template_field_rows,
    validate_template,
    validation_error_message,
    variable_label,
)
from database import Database, DraftConflictError, DraftNotEmptyError, SANCTION_ACTIONS, SANCTION_REASON_CHOICES, SANCTION_REASON_LABELS, dt_from_db, utc_now
from subscriber_preview import (
    SUBSCRIBER_PREVIEW_BY_KEY,
    SUBSCRIBER_PREVIEW_SCENARIOS,
    customization_context_header as format_customization_context_header,
    render_subscriber_preview_scenario,
    subscriber_preview_home_text,
    subscriber_preview_marker,
    subscriber_preview_section_title,
)

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


class ChannelStartCardFlow(StatesGroup):
    text = State()
    text_confirmation = State()
    media = State()
    media_confirmation = State()


class StandardTemplateFlow(StatesGroup):
    edit = State()
    confirmation = State()


class StandardStartCardFlow(StatesGroup):
    media = State()
    media_confirmation = State()


class BroadcastFlow(StatesGroup):
    message = State()
    confirmation = State()


class CustomTransferFlow(StatesGroup):
    import_file = State()
    import_confirmation = State()


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


async def channel_sanction_action_label(db: Database, channel_id: int, action: str) -> str:
    key = f"ui.sanction.action.{action}"
    if key not in TEMPLATE_REGISTRY:
        return SANCTION_ACTION_LABELS.get(action, action)
    return await render_label(db, channel_id, key)


async def channel_sanction_notice_duration(
    db: Database, channel_id: int, action: str, until: datetime | None
) -> str:
    if action == "warning":
        return ""
    if action == "permanent_block":
        return await render_template(db, channel_id, "ui.sanction.duration_permanent")
    if until is not None:
        return await render_template(
            db, channel_id, "ui.sanction.duration_until",
            expires_at=until.strftime("%d.%m.%Y %H:%M UTC"),
        )
    return ""


async def sanction_confirmation_values_channel(
    db: Database, channel_id: int, data: dict[str, object], *, anonymous_tag: str | None = None
) -> dict[str, object]:
    if not sanction_flow_is_complete(data):
        raise ValueError("Incomplete sanction flow")
    reason = Database.resolve_sanction_reason(
        str(data["reason_choice"]),
        data.get("custom_reason") if isinstance(data.get("custom_reason"), str) else None,
    )
    action = str(data["sanction_type"])
    duration = _sanction_duration_seconds(action, data["sanction_parameters"])
    if data["privacy_mode"] == "anonymous":
        target = await render_template(
            db, channel_id, "ui.sanction.target_anonymous",
            anonymous_tag=anonymous_tag or "Аноним",
        )
    else:
        target = await render_template(
            db, channel_id, "ui.sanction.target_identified",
            user_id=int(data["target_user_id"]),
        )
    parameter = ""
    if duration is not None:
        parameter = "\n" + await render_template(
            db, channel_id, "ui.sanction.parameter_duration",
            duration=sanction_duration_text(duration),
        )
    return {
        "target": target,
        "action": await channel_sanction_action_label(db, channel_id, action),
        "parameter": parameter,
        "reason": reason,
        "visible": await render_label(
            db, channel_id,
            "ui.common.yes" if data["show_reason_to_subscriber"] else "ui.common.no",
        ),
    }


def sanction_notification_text(*, event: str, action: str, until: datetime | None, reason: str | None, show_reason: bool) -> str:
    key = f"sanction.{event}.{'visible' if show_reason else 'hidden'}"
    return render_default(key, {
        "action": SANCTION_ACTION_LABELS[action],
        "duration": _sanction_notice_duration(action, until),
        "reason": reason or "",
    })


async def deliver_sanction_notification(*, bot: Bot, user_id: int, action: str, until: datetime | None, reason: str | None, show_reason: bool, db: Database | None = None, channel_id: int | None = None) -> bool:
    key = f"sanction.applied.{'visible' if show_reason else 'hidden'}"
    if db is not None and channel_id is not None:
        values = {
            "action": await channel_sanction_action_label(db, channel_id, action),
            "duration": await channel_sanction_notice_duration(db, channel_id, action, until),
            "reason": reason or "",
        }
        text = await render_template(db, channel_id, key, **values)
    else:
        text = sanction_notification_text(
            event="applied", action=action, until=until, reason=reason, show_reason=show_reason
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


async def sanction_reason_keyboard(db: Database, channel_id: int) -> InlineKeyboardMarkup:
    rows = []
    for choice in SANCTION_REASON_CHOICES:
        key = f"ui.sanction.reason.{choice}"
        label = await render_label(db, channel_id, key) if key in TEMPLATE_REGISTRY else SANCTION_REASON_LABELS.get(choice, "Другое")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"sanction:reason:{choice}")])
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="sanction:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def sanction_visibility_keyboard(db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.yes"), callback_data="sanction:visibility:yes"),
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.no"), callback_data="sanction:visibility:no"),
        ],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="sanction:cancel")],
    ])


async def sanction_confirmation_keyboard(db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.apply"), callback_data="sanction:apply")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="sanction:cancel")],
    ])


async def sanction_duration_keyboard(db: Database, channel_id: int, action: str) -> InlineKeyboardMarkup:
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
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.sanction.other_interval"), callback_data=f"sanction:param:{action}:custom"),
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="sanction:cancel"),
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
STATISTICS_PAGE_LABEL_KEYS = {
    "overview": "ui.statistics.page.overview",
    "messages": "ui.statistics.page.messages",
    "responses": "ui.statistics.page.responses",
    "activity": "ui.statistics.page.activity",
    "top": "ui.statistics.page.top",
    "admins": "ui.statistics.page.admins",
}
STATISTICS_PERIOD_LABEL_KEYS = {
    "today": "ui.statistics.period.today",
    "7d": "ui.statistics.period.7d",
    "30d": "ui.statistics.period.30d",
    "all": "ui.statistics.period.all",
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


async def statistics_text(db: Database, channel_id: int, stats: dict[str, object], page: str) -> str:
    """Render calculated metrics through the channel Custom Pack."""
    if page not in STATISTICS_PAGES:
        page = "overview"
    if page == "overview":
        return await render_template(
            db, channel_id, "statistics.body.overview",
            unique_recipients=int(stats["unique_subscribers"]),
            active_1d=int(stats["active_subscribers_1d"]),
            active_7d=int(stats["active_subscribers_7d"]),
            active_30d=int(stats["active_subscribers_30d"]),
            new_subscribers=int(stats["new_subscribers"]),
            subscriber_messages=int(stats["subscriber_messages"]),
            admin_replies=int(stats["admin_replies"]),
            average_messages_per_subscriber=f"{float(stats['average_messages_per_subscriber']):.2f}",
            conversation_count=int(stats["conversation_count"]),
            answered_count=int(stats["answered_conversation_count"]),
            answered_share=f"{float(stats['answered_conversation_share']):.1f}",
        )
    if page == "messages":
        media = stats["media"]
        return await render_template(
            db, channel_id, "statistics.body.messages",
            text_count=int(media["text"]), photo_count=int(media["photo"]),
            video_count=int(media["video"]), document_count=int(media["document"]),
            voice_count=int(media["voice"]), audio_count=int(media["audio"]),
            sticker_count=int(media["sticker"]), other_count=int(media["other"]),
            album_count=int(stats["album_count"]), media_items_count=int(stats["media_items_count"]),
        )
    if page == "responses":
        return await render_template(
            db, channel_id, "statistics.body.responses",
            conversation_count=int(stats["conversation_count"]),
            answered_count=int(stats["answered_conversation_count"]),
            answered_share=f"{float(stats['answered_conversation_share']):.1f}",
            average_first_response=statistics_duration(stats["average_first_response_seconds"]),
            median_first_response=statistics_duration(stats["median_first_response_seconds"]),
        )
    if page == "activity":
        hours = stats["messages_by_hour"]
        weekdays = stats["messages_by_weekday"]
        weekday_names = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        active_hours = [(hour, int(count)) for hour, count in hours.items() if int(count)]
        top_hours = sorted(active_hours, key=lambda item: (-item[1], item[0]))[:5]
        hours_text = "\n".join(f"{hour:02d}:00 — {count}" for hour, count in top_hours) or STATISTICS_UI_LABELS["no_data"]
        weekdays_text = " · ".join(f"{weekday_names[int(day)]} {int(count)}" for day, count in weekdays.items())
        active_hour = stats["most_active_hour"]
        active_day = stats["most_active_weekday"]
        return await render_template(
            db, channel_id, "statistics.body.activity",
            most_active_hour=f"{int(active_hour):02d}:00" if active_hour is not None else "—",
            most_active_day=weekday_names[int(active_day)] if active_day is not None else "—",
            top_hours=hours_text,
            weekdays=weekdays_text or "—",
        )
    top = stats["top_subscribers"]
    rows = "\n".join(
        f"{position}. {str(row['display_name'])} — {int(row['message_count'])}"
        for position, row in enumerate(top, start=1)
    ) or STATISTICS_UI_LABELS["no_data"]
    return await render_template(db, channel_id, "statistics.body.top", rows=rows)


async def admin_statistics_text(db: Database, channel_id: int, stats: dict[str, object]) -> str:
    admins = stats["admins"]
    handled_conversations = int(
        stats.get("handled_conversation_count", stats.get("answered_conversation_count", 0)) or 0
    )
    tracked_conversations = int(
        stats.get("tracked_conversation_count", stats.get("conversation_count", handled_conversations)) or 0
    )
    unanswered_conversations = int(
        stats.get("unanswered_conversation_count", max(0, tracked_conversations - handled_conversations)) or 0
    )
    rows = "\n".join(
        f"{i}. {str(row['display_name'])} — {int(row['reply_count'])}"
        for i, row in enumerate(admins, 1)
    ) or STATISTICS_UI_LABELS["no_data"]
    return await render_template(
        db, channel_id, "statistics.body.admins",
        active_admin_count=int(stats.get("active_admin_count", 0) or 0),
        admin_replies=int(stats.get("admin_replies", 0) or 0),
        handled_conversations=handled_conversations,
        unanswered_conversations=unanswered_conversations,
        team_average_response=statistics_duration(stats.get("team_average_first_response_seconds")),
        team_median_response=statistics_duration(stats.get("team_median_first_response_seconds")),
        top_reply_admin=str(stats.get("top_reply_admin") or "—"),
        top_first_response_admin=str(stats.get("top_first_response_admin") or "—"),
        rows=rows,
    )


async def render_statistics_page(*, db: Database, channel_id: int, stats: dict[str, object], page: str) -> str:
    """Apply effective channel templates around calculated statistics."""
    key = "statistics.admins" if page == "admins" else f"statistics.page.{page if page in STATISTICS_PAGES else 'overview'}"
    body = await admin_statistics_text(db, channel_id, stats) if page == "admins" else await statistics_text(db, channel_id, stats, page)
    if (page == "admins" and not stats.get("admins")) or (page != "admins" and not int(stats.get("subscriber_messages", 0))):
        body = await render_template(db, channel_id, "statistics.no_data")
    # The outer historical page template accepts body as a normal variable.
    # Strip markup from the independently rendered body before safe insertion.
    body = html.unescape(body).replace("<b>", "").replace("</b>", "")
    legacy_warning = ""
    if not bool(stats.get("conversation_metrics_complete")):
        legacy_warning = "\n\n" + await render_template(db, channel_id, "statistics.legacy_warning")
    page_key = STATISTICS_PAGE_LABEL_KEYS.get(page, STATISTICS_PAGE_LABEL_KEYS["overview"])
    period_key = STATISTICS_PERIOD_LABEL_KEYS.get(str(stats.get("period")), "ui.statistics.period.all")
    return await render_template(
        db, channel_id, key,
        body=body,
        page_title=await render_label(db, channel_id, page_key),
        period=await render_label(db, channel_id, period_key),
        legacy_warning=legacy_warning,
    )


async def statistics_keyboard(*, db: Database, channel_id: int, source: str, page: str = "overview", period: str = "all") -> InlineKeyboardMarkup:
    if source not in {"stats", "panel"}:
        raise ValueError("Unknown statistics source")
    if page not in STATISTICS_PAGES:
        page = "overview"
    if period not in STATISTICS_PERIODS:
        period = "all"
    prefix = "stats" if source == "stats" else "panel:stats"
    callback = lambda next_page, next_period: f"{prefix}:{next_page}:{next_period}"
    page_labels = {key: await render_label(db, channel_id, label_key) for key, label_key in STATISTICS_PAGE_LABEL_KEYS.items()}
    period_labels = {key: await render_label(db, channel_id, label_key) for key, label_key in STATISTICS_PERIOD_LABEL_KEYS.items()}
    page_rows = [
        [InlineKeyboardButton(text=page_labels[key], callback_data=callback(key, period)) for key in STATISTICS_PAGES[:3]],
        [InlineKeyboardButton(text=page_labels[key], callback_data=callback(key, period)) for key in STATISTICS_PAGES[3:5]],
        [InlineKeyboardButton(text=page_labels["admins"], callback_data=callback("admins", period))],
    ]
    period_row = [InlineKeyboardButton(text=period_labels[key], callback_data=callback(page, key)) for key in STATISTICS_PERIODS]
    back = "stats:back" if source == "stats" else "panel:home"
    export_row = [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.statistics.export"), callback_data=f"panel:export:{period}")] if source == "panel" else []
    rows = page_rows + [period_row] + ([export_row] if export_row else [])
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def panel_keyboard(*, db: Database, channel_id: int, show_superadmin_entry: bool = False) -> InlineKeyboardMarkup:
    label = lambda key: render_label(db, channel_id, key)
    rows = [
        [InlineKeyboardButton(text=await label("ui.panel.overview"), callback_data="panel:home"), InlineKeyboardButton(text=await label("ui.panel.statistics"), callback_data="panel:stats")],
        [InlineKeyboardButton(text=await label("ui.panel.cleanup"), callback_data="panel:cleanup"), InlineKeyboardButton(text=await label("ui.panel.notices"), callback_data="panel:notices")],
        [InlineKeyboardButton(text=await label("ui.panel.search"), callback_data="panel:search"), InlineKeyboardButton(text=await label("ui.panel.anonymous"), callback_data="panel:anonymous")],
        [InlineKeyboardButton(text=await label("ui.panel.texts"), callback_data="panel:texts"), InlineKeyboardButton(text=await label("ui.panel.manual_cleanup"), callback_data="panel:manual_cleanup_preview")],
        [InlineKeyboardButton(text=await label("ui.panel.start_card"), callback_data="panel:start_card"), InlineKeyboardButton(text=await label("ui.panel.reactions"), callback_data="panel:reactions")],
        [InlineKeyboardButton(text="Посмотреть глазами подписчика", callback_data=f"preview:subscriber:home:{channel_id}")],
        [InlineKeyboardButton(text=await label("ui.panel.history"), callback_data="panel:custom_history"), InlineKeyboardButton(text=await label("ui.panel.custom_tools"), callback_data="panel:custom_tools")],
        [InlineKeyboardButton(text=await label("ui.panel.custom_transfer"), callback_data="panel:custom_transfer")],
    ]
    # A SUPERADMIN only gets a navigation link into a separate global section.
    # No global profile controls live inside the channel-scoped owner panel.
    if show_superadmin_entry:
        rows.append([InlineKeyboardButton(text="Глобальное управление ботом", callback_data="sa:home")])
    rows.append([InlineKeyboardButton(text=await label("ui.panel.refresh"), callback_data="panel:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def customization_context_text(*, db: Database, channel) -> str:
    """Owner-only context banner used across customization screens."""
    channel_id = int(channel["channel_id"])
    state = await db.get_channel_custom_state(channel_id)
    active_revision_id = int(state["active_revision_id"]) if state is not None else None
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    return format_customization_context_header(
        channel_name=str(channel["group_title"]),
        channel_id=channel_id,
        active_revision_id=active_revision_id,
        draft_count=draft_count,
    )


async def subscriber_preview_keyboard(*, db: Database, channel_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=scenario.title,
            callback_data=f"preview:subscriber:scenario:{scenario.key}:{channel_id}",
        )]
        for scenario in SUBSCRIBER_PREVIEW_SCENARIOS
    ]
    rows.append([InlineKeyboardButton(
        text="Показать все сценарии",
        callback_data=f"preview:subscriber:all:{channel_id}",
    )])
    rows.extend(await custom_draft_control_rows(db=db, channel_id=channel_id))
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.common.back"),
        callback_data="panel:home",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def subscriber_preview_home(*, db: Database, channel) -> tuple[str, InlineKeyboardMarkup]:
    channel_id = int(channel["channel_id"])
    state = await db.get_channel_custom_state(channel_id)
    active_revision_id = int(state["active_revision_id"]) if state is not None else None
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    text = subscriber_preview_home_text(
        channel_name=str(channel["group_title"]),
        channel_id=channel_id,
        active_revision_id=active_revision_id,
        draft_count=draft_count,
    )
    return text, await subscriber_preview_keyboard(db=db, channel_id=channel_id)


async def _subscriber_preview_marker(*, db: Database, channel, scenario_title: str) -> str:
    channel_id = int(channel["channel_id"])
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    return subscriber_preview_marker(
        channel_name=str(channel["group_title"]),
        channel_id=channel_id,
        scenario_title=scenario_title,
        draft_count=draft_count,
    )


async def send_subscriber_preview_scenario(
    *, message: Message, db: Database, channel, scenario_key: str, include_marker: bool = True
) -> bool:
    """Send one read-only subscriber scenario. No subscriber/runtime state is touched."""
    scenario = SUBSCRIBER_PREVIEW_BY_KEY.get(scenario_key)
    if scenario is None:
        raise KeyError(scenario_key)
    channel_id = int(channel["channel_id"])
    if include_marker:
        await message.answer(await _subscriber_preview_marker(
            db=db, channel=channel, scenario_title=scenario.title
        ))
    if scenario_key == "start":
        return await send_channel_start_card(
            message=message, db=db, channel=channel, include_draft=True
        )
    text = await render_subscriber_preview_scenario(
        db=db, channel_id=channel_id, scenario_key=scenario_key,
        notice_text=str(channel["notice_text"]),
    )
    reply_markup = None
    if scenario_key == "privacy":
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=await render_label(db, channel_id, "ui.privacy.anonymous", include_draft=True),
                callback_data=f"preview:subscriber:noop:{channel_id}",
            ),
            InlineKeyboardButton(
                text=await render_label(db, channel_id, "ui.privacy.identified", include_draft=True),
                callback_data=f"preview:subscriber:noop:{channel_id}",
            ),
        ]])
    await message.answer(text, reply_markup=reply_markup)
    return True


async def send_all_subscriber_preview_scenarios(*, message: Message, db: Database, channel) -> bool:
    await message.answer(await _subscriber_preview_marker(
        db=db, channel=channel, scenario_title="Все основные сценарии"
    ))
    media_ok = True
    for scenario in SUBSCRIBER_PREVIEW_SCENARIOS:
        await message.answer(subscriber_preview_section_title(scenario.title))
        ok = await send_subscriber_preview_scenario(
            message=message, db=db, channel=channel,
            scenario_key=scenario.key, include_marker=False,
        )
        media_ok = media_ok and ok
    return media_ok


async def cleanup_keyboard(db: Database, channel_id: int, channel) -> InlineKeyboardMarkup:
    enabled = bool(channel["auto_cleanup_enabled"])
    toggle = InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.cleanup.disable" if enabled else "ui.cleanup.enable"),
        callback_data="panel:cleanup:disable" if enabled else "panel:cleanup:enable_menu",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.basis_created"), callback_data="panel:cleanup:basis:created_at"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.basis_activity"), callback_data="panel:cleanup:basis:last_activity_at")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.scope_all"), callback_data="panel:cleanup:scope:all"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.scope_completed"), callback_data="panel:cleanup:scope:answered_closed")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.action_delete"), callback_data="panel:cleanup:action:delete"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.action_close"), callback_data="panel:cleanup:action:close")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.action_close_delete"), callback_data="panel:cleanup:action:close_then_delete")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")],
    ])


def admin_channel_selection_keyboard(channels) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(channel["group_title"])[:60], callback_data=f"panel:select:{int(channel['channel_id'])}")
    ] for channel in channels])


async def cleanup_enable_keyboard(db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.period_7"), callback_data="panel:cleanup:enable:7"),
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.period_30"), callback_data="panel:cleanup:enable:30"),
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.period_90"), callback_data="panel:cleanup:enable:90"),
    ], [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:cleanup")]])


async def anonymous_settings_keyboard(db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.anonymous.edit_prefix"), callback_data="panel:anonymous:edit")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")],
    ])


async def reaction_settings_keyboard(db: Database, channel_id: int, settings: dict[str, object]) -> InlineKeyboardMarkup:
    has_topic = settings.get("service_topic_id") is not None
    rows = [[
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.reaction.mode1"), callback_data="panel:reactions:mode1"),
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.reaction.mode2"), callback_data="panel:reactions:mode2"),
    ]]
    if has_topic:
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.reaction.rename"), callback_data="panel:reactions:rename")])
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.reaction.recreate"), callback_data="panel:reactions:recreate")])
    else:
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.reaction.create"), callback_data="panel:reactions:create")])
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def reaction_settings_text(db: Database, channel_id: int) -> str:
    settings = await db.get_channel_reaction_settings(channel_id)
    mode = await render_label(db, channel_id, "ui.reaction.mode2" if settings["mode"] == "service" else "ui.reaction.mode1")
    topic = str(settings["service_topic_name"] or await render_label(db, channel_id, "ui.reaction.topic_missing"))
    repair = await render_label(db, channel_id, "ui.reaction.state_repair" if settings["requires_repair"] else "ui.reaction.state_ready")
    return await render_template(db, channel_id, "reaction.settings_overview", mode=mode, topic=topic, repair=repair)



async def custom_draft_control_rows(*, db: Database, channel_id: int) -> list[list[InlineKeyboardButton]]:
    if not await db.has_channel_custom_draft(channel_id):
        return []
    return [
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.publish"), callback_data=f"custom:publish:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.discard"), callback_data=f"custom:discard:{channel_id}")],
    ]


CUSTOM_HISTORY_PAGE_SIZE = 6
CUSTOM_AUDIT_PAGE_SIZE = 8

REVISION_SOURCE_LABELS = {
    "migration_snapshot": "Исходный снимок при миграции",
    "setup_snapshot": "Исходная версия при подключении",
    "stage7_live_snapshot": "Снимок существующего оформления",
    "manual_publish": "Ручная публикация",
    "rollback": "Восстановление предыдущей версии",
    "reset_initial": "Возврат к исходному кастому",
    "apply_current_standard": "Применение актуального стандарта",
    "copy_from_channel": "Копирование из своей предложки",
    "import": "Импорт из JSON",
}

AUDIT_ACTION_LABELS = {
    "draft_published": "Опубликован черновик оформления",
    "draft_discarded": "Удалён неопубликованный черновик",
    "revision_restore_staged": "Версия восстановлена в черновик",
    "draft_foundation_migration": "Система зафиксировала существующее оформление",
    "channel_start_card_media_set": "Изменено медиа стартовой карточки",
    "channel_start_card_media_removed": "Удалено медиа стартовой карточки",
    "schema_defaults_added": "Обновлены системные элементы оформления",
    "initial_reset_staged": "Исходный кастом подготовлен в черновик",
    "current_standard_staged": "Актуальный стандарт подготовлен в черновик",
    "channel_copy_staged": "Кастом другой своей предложки подготовлен в черновик",
    "custom_exported": "Экспортирован JSON кастома",
    "custom_imported": "JSON кастом проверен и подготовлен в черновик",
}


def _custom_history_time(value: object) -> str:
    try:
        return dt_from_db(str(value)).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError):
        return "неизвестно"


def _friendly_custom_item_key(item_key: str) -> str:
    if item_key == "start_card.media":
        return "Медиа стартовой карточки"
    if item_key.startswith("template:"):
        key = item_key[len("template:"):]
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is not None:
            return spec.title
        return "Текст интерфейса"
    return "Системный элемент оформления"


def _revision_summary(source: str, raw_summary: object) -> str:
    if source == "setup_snapshot":
        return "Исходный кастом, созданный при подключении предложки."
    if source == "stage7_live_snapshot":
        return "Снимок оформления, существовавшего до перехода на систему версий."
    if source == "manual_publish":
        return "Опубликован пакет изменений владельца предложки."
    if source == "rollback":
        return "Опубликовано восстановление одной из предыдущих версий."
    if source == "reset_initial":
        return "Опубликован возврат к исходному кастому этой предложки."
    if source == "apply_current_standard":
        return "Опубликован актуальный Standard Custom Pack."
    if source == "copy_from_channel":
        return "Опубликован кастом, скопированный из другой своей предложки."
    value = str(raw_summary or "").strip()
    return value if value else "Системная версия оформления."


async def custom_history_view(*, db: Database, channel, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    channel_id = int(channel["channel_id"])
    total = await db.count_channel_custom_revisions(channel_id)
    state = await db.get_channel_custom_state(channel_id)
    active_revision_id = int(state["active_revision_id"]) if state is not None else 0
    if total <= 0:
        text = await render_template(db, channel_id, "custom.history_empty")
        return text, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")
        ]])
    max_page = max(0, (total - 1) // CUSTOM_HISTORY_PAGE_SIZE)
    page = min(max(0, int(page)), max_page)
    revisions = await db.list_channel_custom_revisions(
        channel_id=channel_id, limit=CUSTOM_HISTORY_PAGE_SIZE, offset=page * CUSTOM_HISTORY_PAGE_SIZE
    )
    text = await render_template(
        db, channel_id, "custom.history_overview",
        channel_name=str(channel["group_title"]), active_revision_id=active_revision_id, count=total,
    )
    text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    if draft_count:
        text += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
    rows: list[list[InlineKeyboardButton]] = []
    for row in revisions:
        revision_id = int(row["revision_id"])
        marker = "● " if revision_id == active_revision_id else ""
        when = _custom_history_time(row["created_at"])[:16]
        rows.append([InlineKeyboardButton(
            text=f"{marker}№{revision_id} · {when}"[:64],
            callback_data=f"custom:revision:{channel_id}:{revision_id}:{page}",
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.common.previous"),
            callback_data=f"custom:history:{channel_id}:{page-1}",
        ))
    if page < max_page:
        nav.append(InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.common.next"),
            callback_data=f"custom:history:{channel_id}:{page+1}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.custom.audit"),
        callback_data=f"custom:audit:{channel_id}:0",
    )])
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home"
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def custom_revision_view(
    *, db: Database, channel, revision_id: int, history_page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    channel_id = int(channel["channel_id"])
    revision = await db.get_channel_custom_revision(channel_id=channel_id, revision_id=revision_id)
    state = await db.get_channel_custom_state(channel_id)
    if revision is None or state is None:
        return None
    active_revision_id = int(state["active_revision_id"])
    diff = await db.diff_channel_custom_revision(channel_id=channel_id, revision_id=revision_id)
    source = str(revision["source"])
    actor = "система" if revision["created_by"] is None else f"Telegram ID {int(revision['created_by'])}"
    text = await render_template(
        db, channel_id, "custom.history_revision",
        revision_id=revision_id,
        status="опубликована сейчас" if revision_id == active_revision_id else "архивная",
        created_at=_custom_history_time(revision["created_at"]),
        source=REVISION_SOURCE_LABELS.get(source, "Системное изменение"),
        actor=actor,
        item_count=int(diff["item_count"]),
        changed_count=len(diff["changed_keys"]),
        summary=_revision_summary(source, revision["summary"]),
    )
    text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
    changed = list(diff["changed_keys"])
    if changed:
        visible = [_friendly_custom_item_key(str(key)) for key in changed[:20]]
        if len(changed) > len(visible):
            visible.append(f"…и ещё {len(changed) - len(visible)}")
        text += "\n\n" + await render_template(
            db, channel_id, "custom.history_changes",
            changes="\n".join(f"• {item}" for item in visible),
        )
    rows = [[InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.custom.preview_revision"),
        callback_data=f"custom:revision_preview:{channel_id}:{revision_id}:{history_page}",
    )]]
    if revision_id != active_revision_id:
        rows.append([InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.custom.restore"),
            callback_data=f"custom:revision_restore:{channel_id}:{revision_id}:{history_page}",
        )])
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.common.back"),
        callback_data=f"custom:history:{channel_id}:{history_page}",
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def custom_audit_view(*, db: Database, channel, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    channel_id = int(channel["channel_id"])
    total = await db.count_customization_audit(channel_id=channel_id, scope_type="channel_custom")
    max_page = max(0, (total - 1) // CUSTOM_AUDIT_PAGE_SIZE) if total else 0
    page = min(max(0, int(page)), max_page)
    text = await render_template(
        db, channel_id, "custom.audit_overview",
        channel_name=str(channel["group_title"]), count=total,
    )
    text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
    events = await db.list_customization_audit(
        channel_id=channel_id, scope_type="channel_custom",
        limit=CUSTOM_AUDIT_PAGE_SIZE, offset=page * CUSTOM_AUDIT_PAGE_SIZE,
    )
    if not events:
        text += "\n\n" + await render_template(db, channel_id, "custom.audit_empty")
    else:
        blocks = []
        for event in events:
            actor = "система" if event["actor_user_id"] is None else f"Telegram ID {int(event['actor_user_id'])}"
            action = AUDIT_ACTION_LABELS.get(str(event["action"]), "Изменено оформление предложки")
            blocks.append(await render_template(
                db, channel_id, "custom.audit_event",
                created_at=_custom_history_time(event["created_at"]), actor=actor, action=action,
            ))
        text += "\n\n" + "\n\n".join(blocks)
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.common.previous"),
            callback_data=f"custom:audit:{channel_id}:{page-1}",
        ))
    if page < max_page:
        nav.append(InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.common.next"),
            callback_data=f"custom:audit:{channel_id}:{page+1}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.custom.revisions"),
        callback_data=f"custom:history:{channel_id}:0",
    )])
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home"
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def custom_tools_view(*, db: Database, channel) -> tuple[str, InlineKeyboardMarkup]:
    channel_id = int(channel["channel_id"])
    state = await db.get_channel_custom_state(channel_id)
    standard = await db.get_standard_custom_state()
    if state is None or standard is None:
        text = await render_template(db, channel_id, "custom.history_unavailable")
        return text, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")
        ]])
    text = await render_template(
        db, channel_id, "custom.tools_overview",
        channel_name=str(channel["group_title"]),
        active_revision_id=int(state["active_revision_id"]),
        initial_revision_id=int(state["initial_revision_id"]),
        standard_revision_id=int(standard["active_revision_id"]),
    )
    text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    if draft_count:
        text += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
    rows = [
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.reset_initial"), callback_data=f"custom:tools:reset_initial:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.apply_standard"), callback_data=f"custom:tools:apply_standard:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.copy_from_channel"), callback_data=f"custom:tools:copy:{channel_id}")],
    ]
    rows.extend(await custom_draft_control_rows(db=db, channel_id=channel_id))
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _custom_plan_changes(plan: dict[str, object]) -> str:
    keys = [str(key) for key in list(plan.get("changed_keys") or [])]
    if not keys:
        return "—"
    visible = [_friendly_custom_item_key(key) for key in keys[:20]]
    if len(keys) > len(visible):
        visible.append(f"…и ещё {len(keys) - len(visible)}")
    return "\n".join(f"• {item}" for item in visible)


async def custom_tools_plan_text(*, db: Database, channel_id: int, title: str, plan: dict[str, object]) -> str:
    return await render_template(
        db, channel_id, "custom.tools_plan",
        title=title,
        count=int(plan.get("staged") or 0),
        skipped=int(plan.get("skipped") or 0),
        changes=_custom_plan_changes(plan),
    )


async def custom_tools_staged_keyboard(*, db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.preview"), callback_data=f"custom:draft_preview:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.publish"), callback_data=f"custom:publish:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.discard"), callback_data=f"custom:discard:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data=f"custom:tools:home:{channel_id}")],
    ])


async def custom_transfer_view(*, db: Database, channel) -> tuple[str, InlineKeyboardMarkup]:
    channel_id = int(channel["channel_id"])
    state = await db.get_channel_custom_state(channel_id)
    if state is None:
        text = await render_template(db, channel_id, "custom.history_unavailable")
        return text, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")
        ]])
    text = await render_template(
        db, channel_id, "custom.transfer_overview",
        channel_name=str(channel["group_title"]),
        channel_id=channel_id,
        revision_id=int(state["active_revision_id"]),
    )
    text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
    draft_count = await db.get_channel_custom_draft_count(channel_id)
    if draft_count:
        text += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
    rows = [
        [InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.custom.export_json"),
            callback_data=f"custom:transfer:export:{channel_id}",
        )],
        [InlineKeyboardButton(
            text=await render_label(db, channel_id, "ui.custom.import_json"),
            callback_data=f"custom:transfer:import:{channel_id}",
        )],
    ]
    rows.extend(await custom_draft_control_rows(db=db, channel_id=channel_id))
    rows.append([InlineKeyboardButton(
        text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home"
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def custom_transfer_plan_text(*, db: Database, channel_id: int, plan: dict[str, object]) -> str:
    source_name = str(plan.get("import_source_channel_title") or "не указано")
    source_revision = plan.get("import_source_revision_id")
    revision_text = "не указана" if source_revision is None else f"№{int(source_revision)}"
    media_state = "есть, file_id проверен" if bool(plan.get("import_has_media")) else "без медиа"
    return await render_template(
        db, channel_id, "custom.transfer_plan",
        source_name=source_name,
        revision_id=revision_text,
        count=int(plan.get("staged") or 0),
        skipped=int(plan.get("skipped") or 0),
        media_state=media_state,
        changes=_custom_plan_changes(plan),
    )


async def custom_transfer_staged_keyboard(*, db: Database, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.preview"), callback_data=f"custom:draft_preview:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.publish"), callback_data=f"custom:publish:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.discard"), callback_data=f"custom:discard:{channel_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data=f"custom:transfer:home:{channel_id}")],
    ])


async def channel_start_card_keyboard(*, db: Database, channel_id: int, has_media: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.start_card.edit_text"), callback_data="panel:start_card:text")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.start_card.replace_media"), callback_data="panel:start_card:media")],
    ]
    if has_media:
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.start_card.remove_media"), callback_data="panel:start_card:media_remove")])
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.preview"), callback_data="panel:start_card:preview")])
    rows.extend(await custom_draft_control_rows(db=db, channel_id=channel_id))
    rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _channel_start_card_text(
    *, db: Database, channel_id: int, channel_name: str, draft_text: str | None = None,
    include_draft: bool = False, revision_id: int | None = None,
) -> str:
    if draft_text is None and revision_id is None:
        return await render_template(
            db, channel_id, "start.greeting", include_draft=include_draft, channel_name=channel_name
        )
    if draft_text is None:
        draft_text = await db.get_channel_custom_template_text(
            channel_id=channel_id, template_key="start.greeting",
            revision_id=revision_id, include_legacy_template_overlay=False,
        )
        if draft_text is None:
            draft_text = TEMPLATE_REGISTRY["start.greeting"].default
    validate_template("start.greeting", draft_text)
    return draft_text.format(channel_name=html.escape(channel_name))


async def send_channel_start_card(
    *,
    message: Message,
    db: Database,
    channel,
    draft_media: tuple[str, str] | None = None,
    draft_text: str | None = None,
    include_draft: bool = False,
    revision_id: int | None = None,
) -> bool:
    """Send channel-specific post-Start media followed by the greeting text.

    Returns False only when configured media could not be delivered.  The text
    is always sent, so a stale Telegram file_id never blocks the subscriber.
    """
    channel_id = int(channel["channel_id"])
    channel_name = str(channel["group_title"])
    text = await _channel_start_card_text(
        db=db, channel_id=channel_id, channel_name=channel_name, draft_text=draft_text,
        include_draft=include_draft, revision_id=revision_id,
    )
    live_media = await db.get_channel_custom_start_card_media(channel_id, revision_id=revision_id)
    persistent_draft = (
        await db.get_channel_custom_draft_start_card_media(channel_id)
        if include_draft and revision_id is None else None
    )
    if draft_media is not None:
        media_type, media_file_id = draft_media
    elif persistent_draft is not None and persistent_draft.get("operation") == "delete":
        media_type = media_file_id = None
    elif persistent_draft is not None and persistent_draft.get("operation") == "set":
        media_type = str(persistent_draft.get("media_type") or "")
        media_file_id = str(persistent_draft.get("media_file_id") or "")
    elif live_media is not None:
        media_type, media_file_id = live_media["media_type"], live_media["media_file_id"]
    else:
        media_type = media_file_id = None

    media_ok = True
    if media_type and media_file_id:
        try:
            if media_type == "photo":
                await message.answer_photo(photo=media_file_id)
            elif media_type == "video":
                await message.answer_video(video=media_file_id)
            elif media_type == "animation":
                await message.answer_animation(animation=media_file_id)
            else:
                media_ok = False
        except TelegramAPIError:
            media_ok = False
            logger.warning(
                "Unable to send channel start-card media channel_id=%s type=%s",
                channel_id, media_type,
            )
    await message.answer(text)
    return media_ok


async def send_channel_start_card_preview(
    *,
    message: Message,
    db: Database,
    channel,
    draft_media: tuple[str, str] | None = None,
    draft_text: str | None = None,
    revision_id: int | None = None,
) -> bool:
    await message.answer(await _subscriber_preview_marker(
        db=db, channel=channel, scenario_title="Стартовая карточка"
    ))
    return await send_channel_start_card(
        message=message, db=db, channel=channel, draft_media=draft_media, draft_text=draft_text,
        include_draft=revision_id is None, revision_id=revision_id,
    )


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


SUPERADMIN_PAGE_SIZE = 8
STANDARD_CATEGORY_PAGE_SIZE = 6


def superadmin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стандарт для новых предложок", callback_data="sa:std")],
        [InlineKeyboardButton(text="Глобальный профиль бота", callback_data="sa:profile")],
        [InlineKeyboardButton(text="Глобальный аудит", callback_data="sa:audit:0")],
        [InlineKeyboardButton(text="Закрыть", callback_data="sa:close")],
    ])


def global_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить Description", callback_data="sa:profile:text")],
        [InlineKeyboardButton(text="Заменить Description Picture", callback_data="sa:profile:media")],
        [InlineKeyboardButton(text="Подготовить медиа для BotFather", callback_data="sa:profile:media_apply")],
        [InlineKeyboardButton(text="Удалить подготовленное медиа", callback_data="sa:profile:media_remove")],
        [InlineKeyboardButton(text="Предпросмотр", callback_data="sa:profile:preview")],
        [InlineKeyboardButton(text="Сбросить Description", callback_data="sa:profile:reset")],
        [InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)],
        [InlineKeyboardButton(text="Назад", callback_data="sa:home")],
    ])


def _standard_categories() -> list[str]:
    return template_categories()


def _standard_specs(category_index: int):
    categories = _standard_categories()
    if category_index < 0 or category_index >= len(categories):
        return None, []
    category = categories[category_index]
    return category, specs_for_category(category)


async def standard_home_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    state = await db.get_standard_custom_state()
    if state is None:
        text = "<b>Стандарт для новых предложок</b>\n\nStandard Custom Pack недоступен."
        return text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="sa:home")]])
    revision_id = int(state["active_revision_id"])
    revision = await db.get_standard_custom_revision(revision_id)
    media = await db.get_standard_custom_start_card_media()
    total_templates = sum(len(specs_for_category(category)) for category in _standard_categories())
    created_at = str(revision["created_at"]) if revision is not None else "—"
    updated_by = state["updated_by"]
    actor = "система" if updated_by is None else str(int(updated_by))
    text = (
        "<b>Стандарт для новых предложок</b>\n\n"
        "Это глобальный шаблон только для <b>новых</b> предложок. "
        "Изменения не затрагивают уже созданные channel_id.\n\n"
        f"Активная версия: <b>#{revision_id}</b>\n"
        f"Текстовых элементов: <b>{total_templates}</b>\n"
        f"Стандартное медиа Start Card: <b>{'есть' if media else 'нет'}</b>\n"
        f"Обновлено: <code>{html.escape(created_at)}</code>\n"
        f"Автор последней активации: <code>{html.escape(actor)}</code>"
    )
    rows = [[InlineKeyboardButton(text=category, callback_data=f"sa:std:cat:{i}:0")]
            for i, category in enumerate(_standard_categories())]
    rows.extend([
        [InlineKeyboardButton(text="Стартовая карточка по умолчанию", callback_data="sa:std:start")],
        [InlineKeyboardButton(text="История Standard Pack", callback_data="sa:std:hist:0")],
        [InlineKeyboardButton(text="Назад", callback_data="sa:home")],
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def standard_category_view(db: Database, *, category_index: int, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    category, specs = _standard_specs(category_index)
    if category is None:
        return None
    max_page = max(0, (len(specs) - 1) // STANDARD_CATEGORY_PAGE_SIZE)
    page = min(max(0, page), max_page)
    start = page * STANDARD_CATEGORY_PAGE_SIZE
    subset = specs[start:start + STANDARD_CATEGORY_PAGE_SIZE]
    rows = []
    for offset, spec in enumerate(subset, start=start):
        rows.append([InlineKeyboardButton(text=spec.title, callback_data=f"sa:std:open:{category_index}:{offset}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"sa:std:cat:{category_index}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"sa:std:cat:{category_index}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Назад", callback_data="sa:std")])
    text = (
        "<b>Standard Custom Pack</b>\n"
        f"Раздел: <b>{html.escape(category)}</b>\n"
        f"Страница {page + 1} из {max_page + 1}\n\n"
        "Редактирование создаёт новую immutable-версию стандарта. "
        "Существующие предложки не меняются."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def standard_history_view(db: Database, *, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_standard_custom_revisions()
    max_page = max(0, (total - 1) // SUPERADMIN_PAGE_SIZE)
    page = min(max(0, page), max_page)
    rows = await db.list_standard_custom_revisions(limit=SUPERADMIN_PAGE_SIZE, offset=page * SUPERADMIN_PAGE_SIZE)
    lines = ["<b>История Standard Custom Pack</b>", f"Версий: <b>{total}</b>", ""]
    for row in rows:
        rid = int(row["revision_id"])
        actor = "система" if row["created_by"] is None else str(int(row["created_by"]))
        source = html.escape(str(row["source"]))
        summary = html.escape(str(row["summary"] or ""))
        lines.append(f"<b>#{rid}</b> · <code>{html.escape(str(row['created_at']))}</code> · {html.escape(actor)}")
        lines.append(f"{source}" + (f" — {summary}" if summary else ""))
        lines.append("")
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"sa:std:hist:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"sa:std:hist:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="sa:std")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=buttons)


async def global_audit_view(db: Database, *, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_global_customization_audit()
    max_page = max(0, (total - 1) // SUPERADMIN_PAGE_SIZE)
    page = min(max(0, page), max_page)
    subset = await db.list_global_customization_audit(
        limit=SUPERADMIN_PAGE_SIZE, offset=page * SUPERADMIN_PAGE_SIZE
    )
    lines = ["<b>Глобальный аудит</b>", f"Событий: <b>{total}</b>", ""]
    for row in subset:
        actor = "система" if row["actor_user_id"] is None else str(int(row["actor_user_id"]))
        scope = "Standard Pack" if str(row["scope_type"]) == "global_standard" else "Global Profile"
        target = f" · {html.escape(str(row['target_key']))}" if row["target_key"] else ""
        lines.append(
            f"<b>#{int(row['event_id'])}</b> · {html.escape(scope)} · <code>{html.escape(str(row['created_at']))}</code>\n"
            f"{html.escape(str(row['action']))}{target} · actor <code>{html.escape(actor)}</code>"
        )
        lines.append("")
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"sa:audit:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"sa:audit:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="sa:home")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=buttons)


def standard_template_fields_keyboard(spec) -> InlineKeyboardMarkup:
    rows = []
    for name, label, _token, required in template_field_rows(spec):
        rows.append([InlineKeyboardButton(
            text=("★ " if required else "+ ") + label,
            callback_data=f"sa:field:{name}",
        )])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="sa:std")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def standard_editor_prompt(spec) -> str:
    rows = template_field_rows(spec)
    lines = [
        f"<b>Редактирование стандарта: {html.escape(spec.title)}</b>",
        "",
        "Отправьте текст обычным сообщением с форматированием Telegram. "
        "После предпросмотра сохранение создаст новую immutable-версию Standard Custom Pack.",
        "",
    ]
    if rows:
        lines.append("<b>Динамические поля:</b>")
        for _name, label, token, required in rows:
            suffix = " — обязательно" if required else ""
            lines.append(f"• {html.escape(label)}{suffix}: <code>{html.escape(token)}</code>")
    else:
        lines.append("Динамических полей нет.")
    return "\n".join(lines)


async def render_standard_template_preview(db: Database, key: str) -> str:
    spec = TEMPLATE_REGISTRY[key]
    text = await db.get_standard_custom_template_text(template_key=key)
    if text is None:
        text = spec.default
    validate_template(key, text)
    safe = {name: html.escape(value) for name, value in preview_values(spec).items()}
    return text.format(**safe)


async def send_standard_start_card_preview(*, message: Message, db: Database, draft_media: tuple[str, str] | None = None) -> bool:
    spec = TEMPLATE_REGISTRY["start.greeting"]
    text = await db.get_standard_custom_template_text(template_key="start.greeting") or spec.default
    validate_template("start.greeting", text)
    safe = {name: html.escape(value) for name, value in preview_values(spec).items()}
    rendered = text.format(**safe)
    media = await db.get_standard_custom_start_card_media()
    media_type = draft_media[0] if draft_media else (media["media_type"] if media else None)
    media_file_id = draft_media[1] if draft_media else (media["media_file_id"] if media else None)
    ok = True
    await message.answer("<b>Предпросмотр стандартной Start Card для новой предложки</b>")
    if media_type and media_file_id:
        try:
            if media_type == "photo":
                await message.answer_photo(photo=media_file_id)
            elif media_type == "video":
                await message.answer_video(video=media_file_id)
            else:
                await message.answer_animation(animation=media_file_id)
        except TelegramAPIError:
            ok = False
    await message.answer(rendered)
    return ok


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


async def broadcast_preview_keyboard(db: Database, channel_id: int, broadcast_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.broadcast.send"), callback_data=f"broadcast:send:{broadcast_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.broadcast.edit"), callback_data=f"broadcast:edit:{broadcast_id}")],
        [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"broadcast:cancel:{broadcast_id}")],
    ])


async def broadcast_resume_keyboard(db: Database, channel_id: int, broadcast_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=await render_label(db, channel_id, "ui.broadcast.resume"), callback_data=f"broadcast:resume:{broadcast_id}")
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
                        action=await channel_sanction_action_label(self.db, first.channel_id, action),
                        duration=await channel_sanction_notice_duration(self.db, first.channel_id, action, until),
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
            try:
                await self.bot.send_message(
                    chat_id=first.user_id,
                    text=await render_template(
                        self.db, first.channel_id, "message.received"
                    ),
                )
            except TelegramAPIError:
                # Delivery to the forum already succeeded; a failed acknowledgement
                # must not duplicate the subscriber message on retry.
                logger.warning(
                    "Unable to deliver subscriber acknowledgement channel=%s user=%s",
                    first.channel_id, first.user_id,
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
    body = await render_template(db, channel_id, "panel.overview",
        channel_name=str(channel["group_title"]), subscribers=subscribers, topics=topics,
        period_days=int(channel["reset_interval_days"]), timezone=str(channel["timezone_name"]),
        next_reset=next_reset.strftime("%d.%m.%Y %H:%M"), deep_link=link,
        notice_text=str(channel["notice_text"]))
    return f"{await customization_context_text(db=db, channel=channel)}\n\n{body}"


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
    channel_id = int(channel["channel_id"])
    enabled = await render_label(
        db, channel_id,
        "ui.cleanup.state_enabled" if bool(channel["auto_cleanup_enabled"]) else "ui.cleanup.state_disabled",
    )
    basis = await render_label(
        db, channel_id,
        "ui.cleanup.basis_activity_text" if channel["cleanup_basis"] == "last_activity_at" else "ui.cleanup.basis_created_text",
    )
    scope = await render_label(
        db, channel_id,
        "ui.cleanup.scope_completed_text" if channel["cleanup_status_scope"] == "answered_closed" else "ui.cleanup.scope_all_text",
    )
    action_key = {
        "delete": "ui.cleanup.action_delete_text",
        "close": "ui.cleanup.action_close_text",
        "close_then_delete": "ui.cleanup.action_close_delete_text",
    }[str(channel["cleanup_action"])]
    return await render_template(
        db, channel_id, "cleanup.overview",
        enabled=enabled, period_days=int(channel["reset_interval_days"]), basis=basis, scope=scope,
        action=await render_label(db, channel_id, action_key),
        final_delete_days=int(channel["cleanup_final_delete_days"]),
    )


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
    global_authorizer = GlobalAuthorizer(
        superadmin_telegram_id=getattr(settings, "superadmin_telegram_id", None)
    )

    async def owner_panel_keyboard(actor_id: int | None, channel_id: int) -> InlineKeyboardMarkup:
        return await panel_keyboard(
            db=db, channel_id=channel_id,
            show_superadmin_entry=global_authorizer.is_superadmin(actor_id),
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
            await message.answer(await render_template(db, first.channel_id, "broadcast.unavailable"))
            return
        decision = await authorizer.require(
            actor_id=first.owner_id, channel_id=first.channel_id, action=ChannelAction.BROADCAST,
            context_group_id=first.group_id, require_current_telegram_admin=True,
        )
        if not decision.allowed:
            await state.clear()
            await message.answer(await render_template(db, first.channel_id, "broadcast.owner_required"))
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
            await message.answer(await render_template(db, first.channel_id, "broadcast.unavailable"))
            return
        await message.answer(
            await render_template(db, first.channel_id, "broadcast.preview_ready"),
            reply_markup=await broadcast_preview_keyboard(db, channel_id, broadcast_id),
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
            await sync_command_menus(
                bot=bot,
                db=db,
                superadmin_telegram_id=settings.superadmin_telegram_id,
            )
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
        # half-configured channel with an accidental default prefix.  Once the
        # prefix is accepted, Database.register_channel also creates the initial
        # Standard Custom Pack snapshot in the same SQLite transaction.
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
            reply_markup=await statistics_keyboard(db=db, channel_id=int(channel["channel_id"]), source="stats"),
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
                await callback.message.edit_text(text, reply_markup=await statistics_keyboard(db=db, channel_id=int(channel["channel_id"]), source="stats", page=data[1], period=data[2]))
        except TelegramBadRequest:
            await callback.answer(render_default("statistics.unavailable", {}), show_alert=True)
            return
        await callback.answer()

    @router.message(Command("superadmin"))
    async def superadmin_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        actor_id = message.from_user.id if message.from_user else None
        decision = global_authorizer.require(actor_id=actor_id, action=GlobalAction.SUPERADMIN_PANEL)
        if not decision.allowed:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("Глобальное управление доступно только в личном чате с ботом.")
            return
        me = await bot.get_me()
        username = f"@{me.username}" if me.username else "этого бота"
        await message.answer(
            "<b>Глобальное управление ботом</b>\n\n"
            f"Бот: <b>{html.escape(username)}</b>\n"
            "Доступ: только <b>SUPERADMIN</b>.\n\n"
            "Здесь отдельно управляются глобальный Telegram-профиль и Standard Custom Pack "
            "для новых предложок. Ни один CHANNEL_OWNER не получает доступ к этому разделу.",
            reply_markup=superadmin_home_keyboard(),
        )

    @router.callback_query(F.data.startswith("sa:"))
    async def superadmin_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or callback.message is None:
            return

        data = callback.data or ""
        if data == "sa:profile" or data.startswith("sa:profile:"):
            required_action = GlobalAction.PRESTART_PROFILE
        elif data == "sa:std" or data.startswith("sa:std:"):
            required_action = GlobalAction.STANDARD_PACK
        else:
            required_action = GlobalAction.SUPERADMIN_PANEL
        decision = global_authorizer.require(
            actor_id=callback.from_user.id, action=required_action
        )
        if not decision.allowed:
            await state.clear()
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if callback.message.chat.type != ChatType.PRIVATE:
            await state.clear()
            await callback.answer("Глобальное управление доступно только в личном чате.", show_alert=True)
            return

        parts = data.split(":")

        if data == "sa:home":
            await state.clear()
            me = await bot.get_me()
            username = f"@{me.username}" if me.username else "этого бота"
            await callback.message.edit_text(
                "<b>Глобальное управление ботом</b>\n\n"
                f"Бот: <b>{html.escape(username)}</b>\n"
                "Доступ: только <b>SUPERADMIN</b>.\n\n"
                "Global Bot Profile и Standard Custom Pack разделены и не зависят от выбранной предложки.",
                reply_markup=superadmin_home_keyboard(),
            )
            await callback.answer()
            return

        if data == "sa:close":
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return

        if data == "sa:profile":
            await state.clear()
            profile_decision = global_authorizer.require(
                actor_id=callback.from_user.id, action=GlobalAction.PRESTART_PROFILE
            )
            if not profile_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            me = await bot.get_me()
            username = f"@{me.username}" if me.username else "—"
            name = getattr(me, "full_name", None) or getattr(me, "first_name", None) or "—"
            description = await effective_prestart_description(bot, db)
            stored = await db.get_bot_prestart_card()
            media_state = "подготовлено" if stored is not None and stored["media_type"] else "нет"
            text = (
                f"<b>Глобальный профиль {html.escape(username)}</b>\n\n"
                "Доступ: только <b>SUPERADMIN</b>.\n"
                "Изменения видят пользователи всего бота. Это не Channel Custom Pack.\n\n"
                f"Имя бота: <b>{html.escape(str(name))}</b>\n"
                f"Username: <code>{html.escape(username)}</code>\n"
                f"Description:\n{html.escape(description)}\n\n"
                f"Description Picture candidate: <b>{media_state}</b>\n\n"
                "Description применяется ботом автоматически. Avatar, имя, About/Bio и фактическая "
                "Description Picture управляются на уровне Telegram-аккаунта бота; для Picture и других "
                "BotFather-managed полей используйте @BotFather."
            )
            await callback.message.edit_text(text, reply_markup=global_profile_keyboard())
            await callback.answer()
            return

        if data == "sa:profile:text":
            profile_decision = global_authorizer.require(
                actor_id=callback.from_user.id, action=GlobalAction.PRESTART_PROFILE
            )
            if not profile_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            await state.clear()
            await state.set_state(PreStartCardFlow.description)
            await state.update_data(prestart_global_superadmin=True)
            await callback.message.answer("Отправьте новый глобальный Description одним сообщением (до 512 символов).")
            await callback.answer()
            return

        if data == "sa:profile:media":
            profile_decision = global_authorizer.require(
                actor_id=callback.from_user.id, action=GlobalAction.PRESTART_PROFILE
            )
            if not profile_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            await state.clear()
            await state.set_state(PreStartCardFlow.media)
            await state.update_data(prestart_global_superadmin=True)
            await callback.message.answer(
                "Отправьте одно фото, видео или GIF/анимацию. Бот сохранит его только как candidate; "
                "фактическую Description Picture затем нужно применить через @BotFather."
            )
            await callback.answer()
            return

        if data == "sa:profile:preview":
            await state.clear()
            await send_prestart_preview(message=callback.message, bot=bot, db=db)
            await callback.answer()
            return

        if data == "sa:profile:media_apply":
            await state.clear()
            stored = await db.get_bot_prestart_card()
            if stored is None or not stored["media_type"] or not stored["media_file_id"]:
                await callback.answer("Сначала сохраните media candidate.", show_alert=True)
                return
            try:
                media_type, media_file_id = validate_media(str(stored["media_type"]), str(stored["media_file_id"]))
                instruction = description_picture_apply_instructions(media_type)
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]])
                if media_type == "photo":
                    await callback.message.answer_photo(photo=media_file_id, caption=instruction, reply_markup=markup)
                elif media_type == "video":
                    await callback.message.answer_video(video=media_file_id, caption=instruction, reply_markup=markup)
                else:
                    await callback.message.answer_animation(animation=media_file_id, caption=instruction, reply_markup=markup)
            except (ValueError, TelegramAPIError):
                await callback.answer("Сохранённое медиа недоступно. Загрузите его заново.", show_alert=True)
                return
            await callback.answer()
            return

        if data == "sa:profile:media_remove":
            await state.clear()
            await db.remove_bot_prestart_media(updated_by=callback.from_user.id)
            await callback.message.answer(
                description_picture_remove_instructions(),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]),
            )
            await callback.answer("Candidate удалён.", show_alert=True)
            return

        if data == "sa:profile:reset":
            await state.clear()
            try:
                await apply_description(bot, DEFAULT_PRESTART_DESCRIPTION)
            except TelegramAPIError:
                await callback.answer("Не удалось применить стандартный Description.", show_alert=True)
                return
            await db.reset_bot_prestart_card(updated_by=callback.from_user.id)
            await callback.message.answer(
                "Стандартный Description восстановлен. Если Description Picture была установлена, удалите её также через @BotFather.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)]]),
            )
            await callback.answer("Глобальный профиль сброшен.", show_alert=True)
            return

        if data == "sa:std":
            std_decision = global_authorizer.require(
                actor_id=callback.from_user.id, action=GlobalAction.STANDARD_PACK
            )
            if not std_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            await state.clear()
            text, keyboard = await standard_home_view(db)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "cat"]:
            await state.clear()
            try:
                category_index, page = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            view = await standard_category_view(db, category_index=category_index, page=page)
            if view is None:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            text, keyboard = view
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "open"]:
            await state.clear()
            try:
                category_index, item_index = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            category, specs = _standard_specs(category_index)
            if category is None or item_index < 0 or item_index >= len(specs):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            spec = specs[item_index]
            current = await db.get_standard_custom_template_text(template_key=spec.key) or spec.default
            try:
                preview = await render_standard_template_preview(db, spec.key)
            except (ValueError, KeyError):
                preview = html.escape(current)
            body = (
                f"<b>{html.escape(spec.title)}</b>\n"
                f"{html.escape(spec.description)}\n"
                f"Где: {html.escape(spec.used_in)}\n"
                f"Аудитория: {html.escape(spec.audience)}\n\n"
                "<b>Текущий стандартный вид:</b>\n" + preview +
                "\n\nСохранение изменит только Standard Custom Pack для будущих /setup."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Изменить", callback_data=f"sa:std:edit:{category_index}:{item_index}"),
                 InlineKeyboardButton(text="Предпросмотр", callback_data=f"sa:std:preview:{category_index}:{item_index}")],
                [InlineKeyboardButton(text="Вернуть default приложения", callback_data=f"sa:std:reset:{category_index}:{item_index}")],
                [InlineKeyboardButton(text="Назад", callback_data=f"sa:std:cat:{category_index}:0")],
            ])
            await callback.message.edit_text(body, reply_markup=kb)
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "preview"]:
            await state.clear()
            try:
                category_index, item_index = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            category, specs = _standard_specs(category_index)
            if category is None or item_index < 0 or item_index >= len(specs):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            spec = specs[item_index]
            await callback.message.answer(
                f"<b>Предпросмотр Standard Pack: {html.escape(spec.title)}</b>\n\n"
                + await render_standard_template_preview(db, spec.key)
            )
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "edit"]:
            try:
                category_index, item_index = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            category, specs = _standard_specs(category_index)
            if category is None or item_index < 0 or item_index >= len(specs):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            spec = specs[item_index]
            await state.clear()
            await state.set_state(StandardTemplateFlow.edit)
            await state.update_data(
                standard_template_key=spec.key,
                standard_category_index=category_index,
                standard_item_index=item_index,
            )
            await callback.message.answer(
                standard_editor_prompt(spec), reply_markup=standard_template_fields_keyboard(spec)
            )
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "reset"]:
            await state.clear()
            try:
                category_index, item_index = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            category, specs = _standard_specs(category_index)
            if category is None or item_index < 0 or item_index >= len(specs):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            spec = specs[item_index]
            await callback.message.answer(
                f"Вернуть «{html.escape(spec.title)}» к default из текущей версии приложения? "
                "Это создаст новую Standard revision и не изменит существующие предложки.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Подтвердить", callback_data=f"sa:std:resetcf:{category_index}:{item_index}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"sa:std:open:{category_index}:{item_index}")],
                ]),
            )
            await callback.answer()
            return

        if len(parts) == 5 and parts[:3] == ["sa", "std", "resetcf"]:
            await state.clear()
            try:
                category_index, item_index = int(parts[3]), int(parts[4])
            except ValueError:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            category, specs = _standard_specs(category_index)
            if category is None or item_index < 0 or item_index >= len(specs):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            result = await db.reset_standard_custom_template_text(
                template_key=specs[item_index].key, updated_by=callback.from_user.id
            )
            if bool(result["changed"]):
                await callback.answer(f"Создана Standard revision #{int(result['revision_id'])}.", show_alert=True)
            else:
                await callback.answer("Этот элемент уже совпадает с default приложения.", show_alert=True)
            return

        if data == "sa:std:start":
            await state.clear()
            state_row = await db.get_standard_custom_state()
            media = await db.get_standard_custom_start_card_media()
            revision_id = int(state_row["active_revision_id"]) if state_row is not None else 0
            text = (
                "<b>Стартовая карточка по умолчанию</b>\n\n"
                "Эту карточку snapshot-копируют только новые предложки при /setup.\n"
                f"Standard revision: <b>#{revision_id}</b>\n"
                f"Медиа: <b>{'есть' if media else 'нет'}</b>\n\n"
                "Текст Start Card редактируется как элемент «Приветствие» в Standard Pack. "
                "Медиа можно задать здесь."
            )
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Предпросмотр", callback_data="sa:std:start_preview")],
                [InlineKeyboardButton(text="Задать медиа", callback_data="sa:std:start_media")],
                [InlineKeyboardButton(text="Удалить медиа", callback_data="sa:std:start_media_remove")],
                [InlineKeyboardButton(text="Редактировать тексты", callback_data="sa:std")],
                [InlineKeyboardButton(text="Назад", callback_data="sa:std")],
            ]))
            await callback.answer()
            return

        if data == "sa:std:start_preview":
            await state.clear()
            ok = await send_standard_start_card_preview(message=callback.message, db=db)
            await callback.answer("Предпросмотр отправлен." if ok else "Текст отправлен, но media file_id недоступен.", show_alert=not ok)
            return

        if data == "sa:std:start_media":
            await state.clear()
            await state.set_state(StandardStartCardFlow.media)
            await state.update_data(standard_media_superadmin=True)
            await callback.message.answer(
                "Отправьте фото, видео или GIF/анимацию для стандартной Channel Start Card. "
                "Сохранение создаст новую Standard revision."
            )
            await callback.answer()
            return

        if data == "sa:std:start_media_remove":
            await state.clear()
            result = await db.remove_standard_custom_start_card_media(updated_by=callback.from_user.id)
            if bool(result["changed"]):
                await callback.answer(f"Медиа удалено. Standard revision #{int(result['revision_id'])}.", show_alert=True)
            else:
                await callback.answer("В Standard Pack уже нет стартового медиа.", show_alert=True)
            return

        if len(parts) == 4 and parts[:3] == ["sa", "std", "hist"]:
            await state.clear()
            try:
                page = int(parts[3])
            except ValueError:
                page = 0
            text, keyboard = await standard_history_view(db, page=page)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return

        if len(parts) == 3 and parts[:2] == ["sa", "audit"]:
            await state.clear()
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
            text, keyboard = await global_audit_view(db, page=page)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return

        if len(parts) == 3 and parts[:2] == ["sa", "field"]:
            variable = parts[2]
            current_state = await state.get_state()
            data_state = await state.get_data()
            if current_state != StandardTemplateFlow.edit.state:
                await callback.answer("Редактор уже закрыт.", show_alert=True)
                return
            key = data_state.get("standard_template_key")
            spec = TEMPLATE_REGISTRY.get(key) if isinstance(key, str) else None
            if spec is None or spec.scope != "channel" or variable not in spec.variables:
                await callback.answer("Поле недоступно.", show_alert=True)
                return
            suffix = " Обязательное поле." if variable in spec.required else " Поле необязательное."
            await callback.message.answer(
                f"<b>{html.escape(variable_label(variable))}</b>.{suffix}\n"
                "Вставьте эту метку в текст:\n"
                f"<code>{html.escape(friendly_placeholder(variable))}</code>"
            )
            await callback.answer()
            return

        await callback.answer("Действие устарело.", show_alert=True)

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
        await message.answer(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=await owner_panel_keyboard(message.from_user.id, int(channel["channel_id"])), disable_web_page_preview=True)

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
        await callback.message.edit_text(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=await owner_panel_keyboard(callback.from_user.id, int(channel["channel_id"])), disable_web_page_preview=True)
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
        new_search = await render_label(db, channel_id, "ui.search.new")
        back = await render_label(db, channel_id, "ui.common.back")
        if not rows:
            return await render_template(db, channel_id, "search.empty"), InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=new_search, callback_data="panel:search")
            ], [InlineKeyboardButton(text=back, callback_data="panel:home")]])
        lines = [await render_template(db, channel_id, "search.results", count=total)]
        result_buttons = []
        for index, row in enumerate(rows):
            status = f" — {str(row['status'])}" if row['status'] else ""
            lines.append(await render_template(
                db, channel_id, "search.result_line",
                display_name=str(row["display_name"]), status=status,
            ))
            buttons = [InlineKeyboardButton(
                text=await render_label(db, channel_id, "ui.search.result", position=index + 1),
                callback_data=f"search:open:{index}",
            )]
            topic_url = forum_topic_url(row.get("group_id"), row.get("topic_id"))
            if topic_url:
                buttons.append(InlineKeyboardButton(text=await render_label(db, channel_id, "ui.search.open"), url=topic_url))
            result_buttons.append(buttons)
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="◀", callback_data="search:page:prev"))
        if (page + 1) * 8 < total:
            nav.append(InlineKeyboardButton(text="▶", callback_data="search:page:next"))
        keyboard = result_buttons + [[InlineKeyboardButton(text=new_search, callback_data="panel:search")]]
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(text=back, callback_data="panel:home")])
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
        if data == "panel:prestart" or data.startswith("panel:prestart:"):
            global_decision = global_authorizer.require(
                actor_id=callback.from_user.id, action=GlobalAction.PRESTART_PROFILE
            )
            await state.clear()
            if not global_decision.allowed:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
                return
            await callback.message.answer(
                "Глобальный профиль вынесен из панели предложки. Откройте /superadmin.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Глобальное управление ботом", callback_data="sa:home")
                ]]),
            )
            await callback.answer("Раздел перенесён в SUPERADMIN.", show_alert=True)
            return
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
                ], [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data=f"panel:stats:overview:{period}")]])
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
            await callback.message.edit_text(await _panel_text(bot=bot, db=db, channel=channel), reply_markup=await owner_panel_keyboard(callback.from_user.id, int(channel["channel_id"])), disable_web_page_preview=True)
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
                await callback.message.edit_text(text, reply_markup=await statistics_keyboard(db=db, channel_id=channel_id, source="panel", page=page, period=period))
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
                reply_markup=await anonymous_settings_keyboard(db, channel_id),
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
                    reply_markup=await reaction_settings_keyboard(db, channel_id, settings),
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
                        reply_markup=await reaction_settings_keyboard(db, channel_id, settings),
                    )
                elif action == "mode2" and settings.get("service_topic_id") is not None and not bool(settings.get("requires_repair")):
                    await db.set_channel_reaction_mode(
                        channel_id=channel_id, mode="service", updated_by=callback.from_user.id
                    )
                    settings = await db.get_channel_reaction_settings(channel_id)
                    await callback.message.edit_text(
                        await render_template(
                            db, channel_id, "reaction.mode_service_set",
                            topic=str(settings.get("service_topic_name") or await render_label(db, channel_id, "ui.reaction.topic_missing")),
                        ),
                        reply_markup=await reaction_settings_keyboard(db, channel_id, settings),
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
        elif data == "panel:custom_tools":
            tools_text, tools_keyboard = await custom_tools_view(db=db, channel=channel)
            await callback.message.edit_text(tools_text, reply_markup=tools_keyboard)
        elif data == "panel:custom_history":
            history_text, history_keyboard = await custom_history_view(
                db=db, channel=channel, page=0
            )
            await callback.message.edit_text(history_text, reply_markup=history_keyboard)
        elif data == "panel:custom_transfer":
            transfer_text, transfer_keyboard = await custom_transfer_view(db=db, channel=channel)
            await callback.message.edit_text(transfer_text, reply_markup=transfer_keyboard)
        elif data == "panel:start_card":
            live_media = await db.get_channel_custom_start_card_media(channel_id)
            draft_media = await db.get_channel_custom_draft_start_card_media(channel_id)
            if draft_media is not None and draft_media.get("operation") == "delete":
                effective_has_media = False
            elif draft_media is not None and draft_media.get("operation") == "set":
                effective_has_media = True
            else:
                effective_has_media = live_media is not None
            overview = await render_template(
                db, channel_id, "start_card.overview",
                channel_name=str(channel["group_title"]),
                channel_id=channel_id,
                media_state=await render_label(db, channel_id, "ui.start_card.media_saved" if effective_has_media else "ui.start_card.media_none"),
            )
            overview = f"{await customization_context_text(db=db, channel=channel)}\n\n{overview}"
            draft_count = await db.get_channel_custom_draft_count(channel_id)
            if draft_count:
                overview += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
            await callback.message.edit_text(
                overview,
                reply_markup=await channel_start_card_keyboard(db=db, channel_id=channel_id, has_media=effective_has_media),
            )
        elif data == "panel:start_card:text":
            if callback.message.chat.type != ChatType.PRIVATE:
                await callback.answer(await render_template(db, channel_id, "start_card.private_required"), show_alert=True); return
            await state.clear()
            await state.set_state(ChannelStartCardFlow.text)
            await state.update_data(start_card_channel_id=channel_id)
            spec = TEMPLATE_REGISTRY["start.greeting"]
            base = await render_template(db, channel_id, "start_card.text_prompt")
            editor_prompt = template_editor_prompt(spec, base)
            editor_prompt = f"{await customization_context_text(db=db, channel=channel)}\n\n{editor_prompt}"
            await callback.message.answer(
                editor_prompt,
                reply_markup=await template_editor_fields_keyboard(
                    spec, channel_id=channel_id, cancel_callback="start_card:cancel"
                ),
            )
        elif data == "panel:start_card:media":
            if callback.message.chat.type != ChatType.PRIVATE:
                await callback.answer(await render_template(db, channel_id, "start_card.private_required"), show_alert=True); return
            await state.clear()
            await state.set_state(ChannelStartCardFlow.media)
            await state.update_data(start_card_channel_id=channel_id)
            media_prompt = await render_template(db, channel_id, "start_card.media_prompt")
            media_prompt = f"{await customization_context_text(db=db, channel=channel)}\n\n{media_prompt}"
            await callback.message.answer(media_prompt)
        elif data == "panel:start_card:preview":
            media_ok = await send_channel_start_card_preview(
                message=callback.message, db=db, channel=channel
            )
            if not media_ok:
                await callback.answer(await render_template(db, channel_id, "start_card.media_stale"), show_alert=True); return
        elif data == "panel:start_card:media_remove":
            await db.stage_channel_custom_start_card_media_removal(
                channel_id=channel_id, updated_by=callback.from_user.id
            )
            await callback.answer(await render_template(db, channel_id, "custom.draft_saved"), show_alert=True)
            return
        elif data == "panel:notices":
            await callback.message.edit_text(await render_template(db, channel_id, "panel.notices", notice_text=str(channel["notice_text"])), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")]]))
        elif data == "panel:texts":
            text = await render_template(db, channel_id, "panel.texts")
            text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
            draft_count = await db.get_channel_custom_draft_count(channel_id)
            if draft_count:
                text += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
            await callback.message.edit_text(
                text, reply_markup=await template_categories_keyboard(channel_id)
            )
        elif data == "panel:cleanup":
            await callback.message.edit_text(await _cleanup_text(db, channel), reply_markup=await cleanup_keyboard(db, channel_id, channel))
        elif data == "panel:cleanup:disable":
            await db.set_auto_cleanup_enabled(channel_id, False)
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=await cleanup_keyboard(db, channel_id, updated))
        elif data == "panel:cleanup:enable_menu":
            await callback.message.edit_text(await render_template(db, channel_id, "cleanup.enable_prompt"), reply_markup=await cleanup_enable_keyboard(db, channel_id))
        elif data.startswith("panel:cleanup:enable:"):
            days = int(data.rsplit(":", 1)[1])
            await db.enable_auto_cleanup(channel_id=channel_id, days=days)
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=await cleanup_keyboard(db, channel_id, updated))
        elif data.startswith("panel:cleanup:basis:") or data.startswith("panel:cleanup:scope:") or data.startswith("panel:cleanup:action:"):
            _, _, kind, value = data.split(":", 3)
            basis, scope, action = str(channel["cleanup_basis"]), str(channel["cleanup_status_scope"]), str(channel["cleanup_action"])
            if kind == "basis": basis = value
            elif kind == "scope": scope = value
            else: action = value
            await db.set_channel_cleanup_policy(channel_id=channel_id, basis=basis, status_scope=scope, action=action, final_delete_days=int(channel["cleanup_final_delete_days"]))
            updated = await db.get_channel_by_id(channel_id)
            await callback.message.edit_text(await _cleanup_text(db, updated), reply_markup=await cleanup_keyboard(db, channel_id, updated))
        elif data == "panel:manual_cleanup_preview":
            rows = await db.topics_created_before(channel_id=channel_id, cutoff=_manual_cutoff(str(channel["timezone_name"])))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.confirm"), callback_data="panel:manual_cleanup_confirm")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.cleanup.confirm_reset"), callback_data="panel:manual_cleanup_confirm_reset_anon")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")],
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
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")]]),
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
            reply_markup=await reaction_settings_keyboard(db, channel_id, settings),
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
            reply_markup=await anonymous_settings_keyboard(db, channel_id),
        )


    # --------------------------------------------------------------
    # Channel-scoped post-Start card editor
    # --------------------------------------------------------------
    async def start_card_state_channel(message: Message, state: FSMContext):
        data = await state.get_data()
        channel_id = data.get("start_card_channel_id")
        if message.from_user is None or not isinstance(channel_id, int):
            return None
        decision = await authorizer.require(
            actor_id=message.from_user.id,
            channel_id=channel_id,
            action=ChannelAction.SETTINGS,
        )
        return decision.channel if decision.allowed else None

    @router.message(ChannelStartCardFlow.text, F.chat.type == ChatType.PRIVATE)
    async def channel_start_card_text_input(message: Message, state: FSMContext) -> None:
        channel = await start_card_state_channel(message, state)
        if channel is None:
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        channel_id = int(channel["channel_id"])
        spec = TEMPLATE_REGISTRY["start.greeting"]
        try:
            draft = formatted_template_draft(message, key="start.greeting")
        except ValueError as exc:
            await message.answer(
                validation_error_message(exc, key="start.greeting"),
                reply_markup=await template_editor_fields_keyboard(
                    spec, channel_id=channel_id, cancel_callback="start_card:cancel"
                ),
            )
            return
        await state.update_data(start_card_text_draft=draft)
        await state.set_state(ChannelStartCardFlow.text_confirmation)
        await send_channel_start_card_preview(
            message=message, db=db, channel=channel, draft_text=draft
        )
        await message.answer(
            await render_template(db, channel_id, "start_card.text_confirm"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.save_draft"), callback_data="start_card:text:save")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="start_card:cancel")],
            ]),
        )

    @router.message(ChannelStartCardFlow.media, F.chat.type == ChatType.PRIVATE)
    async def channel_start_card_media_input(message: Message, state: FSMContext) -> None:
        channel = await start_card_state_channel(message, state)
        if channel is None:
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        channel_id = int(channel["channel_id"])
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
            await message.answer(await render_template(db, channel_id, "start_card.media_prompt")); return
        await state.update_data(
            start_card_media_type=media_type,
            start_card_media_file_id=media_file_id,
        )
        await state.set_state(ChannelStartCardFlow.media_confirmation)
        await send_channel_start_card_preview(
            message=message, db=db, channel=channel,
            draft_media=(media_type, media_file_id),
        )
        await message.answer(
            await render_template(db, channel_id, "start_card.media_confirm"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.save_draft"), callback_data="start_card:media:save")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="start_card:cancel")],
            ]),
        )

    @router.callback_query(F.data.in_({"start_card:text:save", "start_card:media:save", "start_card:cancel"}))
    async def channel_start_card_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id = data.get("start_card_channel_id")
        if callback.from_user is None or not isinstance(channel_id, int):
            await state.clear(); await callback.answer(render_default("panel.unavailable", {}), show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.data == "start_card:cancel":
            await state.clear()
            await callback.answer(await render_template(db, channel_id, "start_card.cancelled"))
            return
        if callback.data == "start_card:text:save" and await state.get_state() == ChannelStartCardFlow.text_confirmation.state:
            draft = data.get("start_card_text_draft")
            if not isinstance(draft, str):
                await state.clear(); await callback.answer(await render_template(db, channel_id, "start_card.stale"), show_alert=True); return
            try:
                validate_template("start.greeting", draft)
            except ValueError as exc:
                await state.clear()
                await callback.answer(
                    validation_error_message(exc, key="start.greeting"), show_alert=True
                )
                return
            await db.set_channel_custom_draft_template_text(
                channel_id=channel_id, template_key="start.greeting",
                custom_text=draft, updated_by=callback.from_user.id,
            )
            await state.clear()
            await callback.answer(await render_template(db, channel_id, "custom.draft_saved"), show_alert=True)
            return
        if callback.data == "start_card:media:save" and await state.get_state() == ChannelStartCardFlow.media_confirmation.state:
            try:
                media_type, media_file_id = validate_media(
                    data.get("start_card_media_type"), data.get("start_card_media_file_id")
                )
            except ValueError:
                await state.clear(); await callback.answer(await render_template(db, channel_id, "start_card.media_stale"), show_alert=True); return
            await db.set_channel_custom_draft_start_card_media(
                channel_id=channel_id, media_type=media_type,
                media_file_id=media_file_id, updated_by=callback.from_user.id,
            )
            await state.clear()
            await callback.answer(await render_template(db, channel_id, "custom.draft_saved"), show_alert=True)
            return
        await state.clear()
        await callback.answer(await render_template(db, channel_id, "start_card.stale"), show_alert=True)


    # --------------------------------------------------------------
    # Global Bot Profile FSM. It is intentionally independent from every
    # channel_id and checks only the configured SUPERADMIN identity.
    async def prestart_actor_authorized(*, actor_id: int | None) -> bool:
        return global_authorizer.require(
            actor_id=actor_id, action=GlobalAction.PRESTART_PROFILE
        ).allowed

    async def prestart_state_authorized(message: Message, state: FSMContext) -> bool:
        data = await state.get_data()
        return (
            bool(data.get("prestart_global_superadmin"))
            and message.chat.type == ChatType.PRIVATE
            and await prestart_actor_authorized(
                actor_id=message.from_user.id if message.from_user else None
            )
        )

    @router.message(PreStartCardFlow.description)
    async def prestart_description_input(message: Message, state: FSMContext) -> None:
        if not await prestart_state_authorized(message, state):
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        try:
            draft = validate_description(message.text or "")
        except ValueError:
            await message.answer("Нужен непустой глобальный Description длиной до 512 символов.")
            return
        await state.update_data(prestart_description_draft=draft)
        await state.set_state(PreStartCardFlow.description_confirmation)
        await send_prestart_preview(message=message, bot=bot, db=db, draft_text=draft)
        await message.answer(
            "Применить этот Description глобально ко всему боту?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Применить", callback_data="gp:text:save")],
                [InlineKeyboardButton(text="Отмена", callback_data="gp:cancel")],
            ]),
        )

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
            await message.answer("Отправьте одно фото, видео или GIF/анимацию.")
            return
        await state.update_data(prestart_media_type=media_type, prestart_media_file_id=media_file_id)
        await state.set_state(PreStartCardFlow.media_confirmation)
        await send_prestart_preview(
            message=message, bot=bot, db=db, draft_media=(media_type, media_file_id)
        )
        await message.answer(
            "Сохранить это медиа как SUPERADMIN candidate для Description Picture? "
            "Фактическое применение затем выполняется через @BotFather.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Сохранить", callback_data="gp:media:save")],
                [InlineKeyboardButton(text="Отмена", callback_data="gp:cancel")],
            ]),
        )

    @router.callback_query(F.data.in_({"gp:text:save", "gp:media:save", "gp:cancel"}))
    async def prestart_card_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or callback.message is None:
            await state.clear(); return
        data = await state.get_data()
        if not bool(data.get("prestart_global_superadmin")) or not await prestart_actor_authorized(actor_id=callback.from_user.id):
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.message.chat.type != ChatType.PRIVATE:
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.data == "gp:cancel":
            await state.clear(); await callback.answer("Изменение отменено."); return
        if callback.data == "gp:text:save" and await state.get_state() == PreStartCardFlow.description_confirmation.state:
            draft = data.get("prestart_description_draft")
            try:
                normalized = validate_description(draft if isinstance(draft, str) else "")
                await apply_description(bot, normalized)
            except (ValueError, TelegramAPIError):
                await state.clear(); await callback.answer("Description не применён.", show_alert=True); return
            await db.set_bot_prestart_description(description=normalized, updated_by=callback.from_user.id)
            await state.clear(); await callback.answer("Глобальный Description применён.", show_alert=True); return
        if callback.data == "gp:media:save" and await state.get_state() == PreStartCardFlow.media_confirmation.state:
            try:
                media_type, media_file_id = validate_media(
                    data.get("prestart_media_type"), data.get("prestart_media_file_id")
                )
            except ValueError:
                await state.clear(); await callback.answer("Медиа устарело. Загрузите его заново.", show_alert=True); return
            await db.set_bot_prestart_media(
                media_type=media_type, media_file_id=media_file_id, updated_by=callback.from_user.id
            )
            await state.clear()
            await callback.message.answer(
                description_picture_apply_instructions(media_type),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Открыть @BotFather", url=BOTFATHER_URL)
                ]]),
            )
            await callback.answer("Media candidate сохранён.", show_alert=True); return
        await state.clear(); await callback.answer("Действие устарело.", show_alert=True)

    # Standard Custom Pack editor FSM. Saving is an explicit atomic publish to
    # a new immutable Standard revision; it never edits existing channel packs.
    @router.message(StandardTemplateFlow.edit)
    async def standard_template_edit_text(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE or not global_authorizer.require(
            actor_id=message.from_user.id, action=GlobalAction.STANDARD_PACK
        ).allowed:
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        data = await state.get_data()
        key = data.get("standard_template_key")
        spec = TEMPLATE_REGISTRY.get(key) if isinstance(key, str) else None
        if spec is None or spec.scope != "channel":
            await state.clear(); await message.answer("Редактор устарел. Откройте /superadmin заново."); return
        try:
            draft = formatted_template_draft(message, key=spec.key)
        except ValueError as exc:
            await message.answer(
                validation_error_message(exc, key=spec.key),
                reply_markup=standard_template_fields_keyboard(spec),
            )
            return
        await state.update_data(standard_template_draft=draft)
        await state.set_state(StandardTemplateFlow.confirmation)
        safe = {name: html.escape(value) for name, value in preview_values(spec).items()}
        await message.answer(
            "<b>Предпросмотр новой Standard revision</b>\n\n" + draft.format(**safe),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Опубликовать в Standard Pack", callback_data="stdedit:save")],
                [InlineKeyboardButton(text="Отмена", callback_data="stdedit:cancel")],
            ]),
        )

    @router.callback_query(F.data.in_({"stdedit:save", "stdedit:cancel"}))
    async def standard_template_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not global_authorizer.require(
            actor_id=callback.from_user.id, action=GlobalAction.STANDARD_PACK
        ).allowed:
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.data == "stdedit:cancel":
            await state.clear(); await callback.answer("Изменение стандарта отменено."); return
        data = await state.get_data()
        key, draft = data.get("standard_template_key"), data.get("standard_template_draft")
        if await state.get_state() != StandardTemplateFlow.confirmation.state or not isinstance(key, str) or not isinstance(draft, str):
            await state.clear(); await callback.answer("Редактор устарел.", show_alert=True); return
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None or spec.scope != "channel":
            await state.clear(); await callback.answer("Элемент не относится к Standard Custom Pack.", show_alert=True); return
        try:
            validate_template(key, draft)
            result = await db.publish_standard_custom_template_text(
                template_key=key, custom_text=draft, updated_by=callback.from_user.id
            )
        except ValueError as exc:
            await state.clear(); await callback.answer(validation_error_message(exc, key=key), show_alert=True); return
        await state.clear()
        if bool(result["changed"]):
            await callback.answer(f"Создана Standard revision #{int(result['revision_id'])}.", show_alert=True)
        else:
            await callback.answer("Текст не изменился; новая версия не создана.", show_alert=True)

    @router.message(StandardStartCardFlow.media)
    async def standard_start_card_media_input(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.chat.type != ChatType.PRIVATE or not global_authorizer.require(
            actor_id=message.from_user.id, action=GlobalAction.STANDARD_PACK
        ).allowed:
            await state.clear(); await message.answer(ACCESS_DENIED_TEXT); return
        data = await state.get_data()
        if not bool(data.get("standard_media_superadmin")):
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
            await message.answer("Отправьте фото, видео или GIF/анимацию."); return
        await state.update_data(standard_media_type=media_type, standard_media_file_id=media_file_id)
        await state.set_state(StandardStartCardFlow.media_confirmation)
        await send_standard_start_card_preview(
            message=message, db=db, draft_media=(media_type, media_file_id)
        )
        await message.answer(
            "Сохранить это медиа в Standard Custom Pack для новых предложок?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Опубликовать в Standard Pack", callback_data="stdmedia:save")],
                [InlineKeyboardButton(text="Отмена", callback_data="stdmedia:cancel")],
            ]),
        )

    @router.callback_query(F.data.in_({"stdmedia:save", "stdmedia:cancel"}))
    async def standard_start_card_media_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not global_authorizer.require(
            actor_id=callback.from_user.id, action=GlobalAction.STANDARD_PACK
        ).allowed:
            await state.clear(); await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.data == "stdmedia:cancel":
            await state.clear(); await callback.answer("Изменение стандарта отменено."); return
        data = await state.get_data()
        if await state.get_state() != StandardStartCardFlow.media_confirmation.state or not bool(data.get("standard_media_superadmin")):
            await state.clear(); await callback.answer("Редактор устарел.", show_alert=True); return
        try:
            media_type, media_file_id = validate_media(
                data.get("standard_media_type"), data.get("standard_media_file_id")
            )
            result = await db.set_standard_custom_start_card_media(
                media_type=media_type, media_file_id=media_file_id, updated_by=callback.from_user.id
            )
        except ValueError:
            await state.clear(); await callback.answer("Медиа устарело. Загрузите его заново.", show_alert=True); return
        await state.clear()
        if bool(result["changed"]):
            await callback.answer(f"Создана Standard revision #{int(result['revision_id'])}.", show_alert=True)
        else:
            await callback.answer("Медиа не изменилось; новая версия не создана.", show_alert=True)

    # Channel template editor (private owner panel)
    # --------------------------------------------------------------
    async def template_owner(callback: CallbackQuery):
        return await _panel_callback_channel(callback, authorizer)

    async def template_categories_keyboard(channel_id: int) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=category, callback_data=f"template:category:{index}:0")] for index, category in enumerate(template_categories())]
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.template.reset_all"), callback_data="template:reset_all")])
        rows.extend(await custom_draft_control_rows(db=db, channel_id=channel_id))
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data="panel:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def template_editor_fields_keyboard(spec, *, channel_id: int, cancel_callback: str) -> InlineKeyboardMarkup:
        rows = []
        for name, label, _token, required in template_field_rows(spec):
            prefix = "★ " if required else "+ "
            rows.append([InlineKeyboardButton(
                text=prefix + label, callback_data=f"template:field:{name}"
            )])
        rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=cancel_callback)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def template_fields_description(spec) -> str:
        rows = template_field_rows(spec)
        if not rows:
            return "<b>Динамические поля:</b> нет. Просто отправьте нужный текст."
        lines = ["<b>Динамические поля:</b>"]
        for _name, label, token, required in rows:
            suffix = " — обязательно" if required else " — необязательно"
            lines.append(
                f"• {html.escape(label)}{suffix}: <code>{html.escape(token)}</code>"
            )
        return "\n".join(lines)

    def template_editor_prompt(spec, base_text: str) -> str:
        return (
            f"{base_text}\n\n"
            "<b>Редактор без кода</b>\n"
            "Напишите текст как обычное сообщение и используйте встроенное форматирование Telegram "
            "(жирный, курсив, ссылки и другое). HTML-теги писать не нужно.\n\n"
            + template_fields_description(spec)
            + "\n\nНажмите кнопку динамического поля, чтобы получить его понятную метку для вставки. "
              "Технические имена переменных знать не требуется."
        )

    def formatted_template_draft(message: Message, *, key: str) -> str:
        if message.text is None:
            raise TemplateValidationError("empty", "Template text is empty")
        # aiogram generates safe Telegram HTML from message entities. Raw tags
        # typed by an owner are escaped, while native Telegram formatting is preserved.
        rich_text = message.html_text or html.escape(message.text)
        draft = normalize_editor_template(key, rich_text)
        validate_template(key, draft)
        return draft

    def template_field_help_text(*, name: str, required: bool) -> str:
        # The caller already validates the field against its active spec.
        suffix = " Обязательное поле." if required else " Поле необязательное."
        return (
            f"<b>{html.escape(variable_label(name))}</b>.{suffix}\n"
            "Вставьте эту метку в нужное место обычного текста:\n"
            f"<code>{html.escape(friendly_placeholder(name))}</code>"
        )

    async def render_template_draft(key: str, text: str) -> str:
        spec = TEMPLATE_REGISTRY[key]
        safe = {name: html.escape(value) for name, value in preview_values(spec).items()}
        return text.format(**safe)

    @router.callback_query(F.data.startswith("template:category:"))
    async def template_category(callback: CallbackQuery) -> None:
        channel = await template_owner(callback)
        if channel is None or callback.message is None: return
        parts=(callback.data or "").split(":")
        try: category=template_categories()[int(parts[2])]; page=max(0,int(parts[3]))
        except (IndexError, ValueError): await callback.answer(ACCESS_DENIED_TEXT,show_alert=True); return
        specs=specs_for_category(category); page=min(page,max(0,(len(specs)-1)//6)); subset=specs[page*6:(page+1)*6]
        draft_keys=await db.list_channel_custom_draft_template_keys(int(channel["channel_id"]))
        rows=[[InlineKeyboardButton(text=("✎ " if spec.key in draft_keys else "")+spec.title,callback_data=f"template:open:{spec.key}")] for spec in subset]
        nav=[]
        if page: nav.append(InlineKeyboardButton(text="◀",callback_data=f"template:category:{parts[2]}:{page-1}"))
        if (page+1)*6<len(specs): nav.append(InlineKeyboardButton(text="▶",callback_data=f"template:category:{parts[2]}:{page+1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.back"),callback_data="template:home")])
        channel_id = int(channel["channel_id"])
        body = await render_template(db, channel_id, "template_ui.category_page", category=category, page=page + 1)
        body = f"{await customization_context_text(db=db, channel=channel)}\n\n{body}"
        await callback.message.edit_text(body, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()

    @router.callback_query(F.data == "template:home")
    async def template_home(callback: CallbackQuery) -> None:
        channel=await template_owner(callback)
        if channel is None or callback.message is None: return
        channel_id = int(channel["channel_id"])
        text = await render_template(db, channel_id, "template_ui.home")
        text = f"{await customization_context_text(db=db, channel=channel)}\n\n{text}"
        draft_count = await db.get_channel_custom_draft_count(channel_id)
        if draft_count:
            text += "\n\n" + await render_template(db, channel_id, "custom.draft_status", count=draft_count)
        await callback.message.edit_text(text, reply_markup=await template_categories_keyboard(channel_id)); await callback.answer()

    @router.callback_query(F.data.startswith("template:open:"))
    async def template_open(callback: CallbackQuery) -> None:
        channel = await template_owner(callback)
        if channel is None or callback.message is None:
            return
        key = (callback.data or "").split(":", 2)[-1]
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        channel_id = int(channel["channel_id"])
        current = await db.get_channel_custom_template_text(
            channel_id=channel_id, template_key=key, include_legacy_template_overlay=False
        )
        if current is None:
            current = spec.default
        draft = await db.get_channel_custom_draft_template_text(
            channel_id=channel_id, template_key=key
        )
        state_label = await render_label(
            db, channel_id, "ui.template.state_draft" if draft is not None else "ui.template.state_standard"
        )
        try:
            current_preview = await render_template_draft(key, current)
        except (ValueError, KeyError):
            current_preview = html.escape(current)
        body = (
            f"<b>{html.escape(spec.title)}</b> ({state_label})\n"
            f"{html.escape(spec.description)}\n"
            f"Где: {html.escape(spec.used_in)}\n"
            f"Аудитория: {html.escape(spec.audience)}\n\n"
            f"{template_fields_description(spec)}\n\n"
            "<b>Опубликованный вид:</b>\n"
            f"{current_preview}"
        )
        body = f"{await customization_context_text(db=db, channel=channel)}\n\n{body}"
        if draft is not None:
            try:
                draft_preview = await render_template_draft(key, draft)
            except (ValueError, KeyError):
                draft_preview = html.escape(draft)
            body += "\n\n<b>Черновик:</b>\n" + draft_preview
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.edit"), callback_data=f"template:edit:{key}"),
                InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.preview"), callback_data=f"template:preview:{key}"),
            ],
            [InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.template.reset_one"), callback_data=f"template:reset:{key}")],
            [InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.back"), callback_data="template:home")],
        ])
        await callback.message.edit_text(body, reply_markup=kb)
        await callback.answer()

    @router.callback_query(F.data.startswith("template:preview:"))
    async def template_preview(callback: CallbackQuery) -> None:
        channel=await template_owner(callback)
        if channel is None or callback.message is None:return
        key=(callback.data or "").split(":",2)[-1]; spec=TEMPLATE_REGISTRY.get(key)
        if spec is None: await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        preview=await render_template(db,int(channel["channel_id"]),key,include_draft=True,**preview_values(spec))
        preview_label = await render_label(db, int(channel["channel_id"]), "ui.common.preview")
        await callback.message.answer(f"<b>{html.escape(preview_label)}</b>\n\n{preview}"); await callback.answer()

    @router.callback_query(F.data.startswith("template:edit:"))
    async def template_edit(callback: CallbackQuery, state: FSMContext) -> None:
        channel = await template_owner(callback)
        key = (callback.data or "").split(":", 2)[-1]
        spec = TEMPLATE_REGISTRY.get(key)
        if channel is None or spec is None or callback.message is None:
            return
        await state.clear()
        await state.set_state(TemplateFlow.edit)
        await state.update_data(
            template_channel_id=int(channel["channel_id"]), template_key=key
        )
        base = await render_template(
            db, int(channel["channel_id"]), "template_ui.edit_prompt"
        )
        editor_prompt = template_editor_prompt(spec, base)
        editor_prompt = f"{await customization_context_text(db=db, channel=channel)}\n\n{editor_prompt}"
        await callback.message.answer(
            editor_prompt,
            reply_markup=await template_editor_fields_keyboard(
                spec, channel_id=int(channel["channel_id"]), cancel_callback="template:cancel"
            ),
        )
        await callback.answer()

    async def template_state_channel(message:Message,state:FSMContext):
        data=await state.get_data(); cid=data.get("template_channel_id")
        if message.from_user is None or not isinstance(cid,int): return None
        decision=await authorizer.require(actor_id=message.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        return decision.channel if decision.allowed else None

    @router.callback_query(F.data.startswith("template:field:"))
    async def template_field_help(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or callback.message is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        variable = (callback.data or "").split(":", 2)[-1]
        data = await state.get_data()
        current_state = await state.get_state()
        if current_state == TemplateFlow.edit.state:
            channel_id = data.get("template_channel_id")
            key = data.get("template_key")
        elif current_state == ChannelStartCardFlow.text.state:
            channel_id = data.get("start_card_channel_id")
            key = "start.greeting"
        else:
            await callback.answer(render_default("template_ui.stale", {}), show_alert=True)
            return
        if not isinstance(channel_id, int) or not isinstance(key, str):
            await callback.answer(render_default("template_ui.stale", {}), show_alert=True)
            return
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None or variable not in spec.variables:
            await callback.answer(render_default("template_ui.stale", {}), show_alert=True)
            return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        await callback.message.answer(
            template_field_help_text(name=variable, required=variable in spec.required)
        )
        await callback.answer()

    @router.message(TemplateFlow.edit)
    async def template_edit_text(message: Message, state: FSMContext) -> None:
        channel = await template_state_channel(message, state)
        data = await state.get_data()
        key = data.get("template_key")
        if channel is None or not isinstance(key, str):
            await state.clear()
            await message.answer(ACCESS_DENIED_TEXT)
            return
        spec = TEMPLATE_REGISTRY.get(key)
        if spec is None:
            await state.clear()
            await message.answer(render_default("template_ui.stale", {}))
            return
        try:
            draft = formatted_template_draft(message, key=key)
        except ValueError as exc:
            await message.answer(
                validation_error_message(exc, key=key),
                reply_markup=await template_editor_fields_keyboard(
                    spec, channel_id=int(channel["channel_id"]), cancel_callback="template:cancel"
                ),
            )
            return
        await state.update_data(template_draft=draft)
        await state.set_state(TemplateFlow.confirmation)
        channel_id = int(channel["channel_id"])
        preview_label = await render_label(db, channel_id, "ui.common.preview")
        await message.answer(
            f"<b>{html.escape(preview_label)}</b>\n\n{await render_template_draft(key, draft)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.custom.save_draft"), callback_data="template:save")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="template:cancel")],
            ]),
        )

    @router.callback_query(F.data == "template:save")
    async def template_save(callback:CallbackQuery,state:FSMContext)->None:
        data=await state.get_data(); cid,key,draft=data.get("template_channel_id"),data.get("template_key"),data.get("template_draft")
        if callback.from_user is None or not isinstance(cid,int) or not isinstance(key,str) or not isinstance(draft,str) or await state.get_state()!=TemplateFlow.confirmation.state:
            await state.clear(); await callback.answer(render_default("template_ui.stale", {}), show_alert=True);return
        decision=await authorizer.require(actor_id=callback.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        if not decision.allowed: await state.clear();await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        try:
            validate_template(key, draft)
        except ValueError as exc:
            await state.clear()
            await callback.answer(validation_error_message(exc, key=key), show_alert=True)
            return
        await db.set_channel_custom_draft_template_text(channel_id=cid,template_key=key,custom_text=draft,updated_by=callback.from_user.id); await state.clear(); await callback.answer(await render_template(db, cid, "custom.draft_saved"))

    @router.callback_query(F.data.startswith("template:reset:"))
    async def template_reset_prompt(callback:CallbackQuery,state:FSMContext)->None:
        channel=await template_owner(callback); key=(callback.data or "").split(":",2)[-1]
        if channel is None or key not in TEMPLATE_REGISTRY:return
        await state.clear();await state.set_state(TemplateFlow.reset_one);await state.update_data(template_channel_id=int(channel["channel_id"]),template_key=key)
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.reset_one_prompt"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.confirm"),callback_data="template:reset_confirm")],[InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.cancel"),callback_data="template:cancel")]]));await callback.answer()

    @router.callback_query(F.data == "template:reset_all")
    async def template_reset_all_prompt(callback:CallbackQuery,state:FSMContext)->None:
        channel=await template_owner(callback)
        if channel is None:return
        await state.clear();await state.set_state(TemplateFlow.reset_all);await state.update_data(template_channel_id=int(channel["channel_id"]))
        await callback.message.answer(await render_template(db, int(channel["channel_id"]), "template_ui.reset_all_prompt"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.confirm"),callback_data="template:reset_all_confirm")],[InlineKeyboardButton(text=await render_label(db, int(channel["channel_id"]), "ui.common.cancel"),callback_data="template:cancel")]]));await callback.answer()

    @router.callback_query(F.data.in_({"template:reset_confirm","template:reset_all_confirm","template:cancel"}))
    async def template_confirm_reset(callback:CallbackQuery,state:FSMContext)->None:
        data=await state.get_data(); cid=data.get("template_channel_id")
        if callback.from_user is None or not isinstance(cid,int):await state.clear();await callback.answer(render_default("template_ui.stale", {}), show_alert=True);return
        if callback.data=="template:cancel": await state.clear();await callback.answer(await render_template(db, cid, "template_ui.cancelled"));return
        decision=await authorizer.require(actor_id=callback.from_user.id,channel_id=cid,action=ChannelAction.SETTINGS)
        if not decision.allowed:await state.clear();await callback.answer(ACCESS_DENIED_TEXT,show_alert=True);return
        if callback.data=="template:reset_confirm" and await state.get_state()==TemplateFlow.reset_one.state:
            key=data.get("template_key");
            if isinstance(key,str): await db.stage_channel_custom_template_reset(channel_id=cid,template_key=key,updated_by=callback.from_user.id)
        elif callback.data=="template:reset_all_confirm" and await state.get_state()==TemplateFlow.reset_all.state:
            await db.stage_all_channel_custom_template_resets(channel_id=cid,updated_by=callback.from_user.id)
        else: await state.clear();await callback.answer(await render_template(db, cid, "template_ui.stale"), show_alert=True);return
        await state.clear();await callback.answer(await render_template(db, cid, "template_ui.reset_done"))

    # --------------------------------------------------------------
    # Persistent Channel Custom Pack draft publication / discard
    # --------------------------------------------------------------
    @router.callback_query(F.data.startswith("custom:publish:"))
    async def custom_publish_prompt(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        count = await db.get_channel_custom_draft_count(channel_id)
        if not count:
            await callback.answer(await render_template(db, channel_id, "custom.publish_empty"), show_alert=True); return
        await callback.message.answer(
            await render_template(db, channel_id, "custom.publish_prompt", count=count),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data=f"custom:publish_confirm:{channel_id}")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"custom:cancel:{channel_id}")],
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:publish_confirm:"))
    async def custom_publish_confirm(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        try:
            revision_id = await db.publish_channel_custom_draft(
                channel_id=channel_id, published_by=callback.from_user.id
            )
        except DraftConflictError:
            await callback.answer(await render_template(db, channel_id, "custom.publish_conflict"), show_alert=True); return
        except ValueError:
            await callback.answer(await render_template(db, channel_id, "custom.publish_empty"), show_alert=True); return
        await callback.answer(
            await render_template(db, channel_id, "custom.publish_done", revision_id=revision_id),
            show_alert=True,
        )

    @router.callback_query(F.data.startswith("custom:discard:"))
    async def custom_discard_prompt(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        count = await db.get_channel_custom_draft_count(channel_id)
        if not count:
            await callback.answer(await render_template(db, channel_id, "custom.publish_empty"), show_alert=True); return
        await callback.message.answer(
            await render_template(db, channel_id, "custom.discard_prompt", count=count),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data=f"custom:discard_confirm:{channel_id}")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"custom:cancel:{channel_id}")],
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:discard_confirm:"))
    async def custom_discard_confirm(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        await db.discard_channel_custom_draft(channel_id=channel_id, discarded_by=callback.from_user.id)
        await callback.answer(await render_template(db, channel_id, "custom.discard_done"), show_alert=True)

    @router.callback_query(F.data.startswith("custom:cancel:"))
    async def custom_cancel(callback: CallbackQuery) -> None:
        await callback.answer()

    # --------------------------------------------------------------
    # Safe Channel Custom Pack JSON export/import. Import never writes live.
    # --------------------------------------------------------------
    @router.callback_query(F.data.startswith("custom:transfer:"))
    async def custom_transfer_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        action = parts[2]
        try:
            channel_id = int(parts[3])
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return

        if action == "home":
            current_state = await state.get_state()
            if current_state in {CustomTransferFlow.import_file.state, CustomTransferFlow.import_confirmation.state}:
                await state.clear()
            text, keyboard = await custom_transfer_view(db=db, channel=decision.channel)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer(); return

        if action == "export":
            try:
                document = await db.export_channel_custom_pack(
                    channel_id=channel_id, exported_by=callback.from_user.id
                )
            except (PermissionError, ValueError):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
            payload = dumps_export_document(document)
            filename = f"channel-custom-{channel_id}-v{CUSTOM_PACK_SCHEMA_VERSION}.json"
            caption = await render_template(
                db, channel_id, "custom.transfer_export_caption",
                schema_version=CUSTOM_PACK_SCHEMA_VERSION,
            )
            await bot.send_document(
                chat_id=callback.message.chat.id,
                document=BufferedInputFile(payload, filename=filename),
                caption=caption,
            )
            await callback.answer(); return

        if action == "import":
            if await db.has_channel_custom_draft(channel_id):
                await callback.answer(
                    await render_template(db, channel_id, "custom.transfer_draft_exists"),
                    show_alert=True,
                ); return
            await state.clear()
            await state.set_state(CustomTransferFlow.import_file)
            await state.update_data(import_channel_id=channel_id)
            await callback.message.answer(
                await render_template(db, channel_id, "custom.transfer_import_prompt"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, channel_id, "ui.custom.cancel_import"),
                        callback_data=f"custom:transfer:cancel_import:{channel_id}",
                    )
                ]]),
            )
            await callback.answer(); return

        if action == "cancel_import":
            await state.clear()
            await callback.message.edit_text(
                await render_template(db, channel_id, "custom.transfer_cancelled"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, channel_id, "ui.common.back"),
                        callback_data=f"custom:transfer:home:{channel_id}",
                    )
                ]]),
            )
            await callback.answer(); return

        if action == "confirm_import":
            data = await state.get_data()
            if (
                await state.get_state() != CustomTransferFlow.import_confirmation.state
                or int(data.get("import_channel_id") or 0) != channel_id
                or not isinstance(data.get("import_document"), dict)
            ):
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
            try:
                pack = normalize_import_document(
                    data["import_document"], raw_sha256=str(data.get("import_sha256") or "") or None
                )
                if pack.media_file_id is not None:
                    await bot.get_file(pack.media_file_id)
                result = await db.stage_channel_custom_import(
                    channel_id=channel_id, imported_by=callback.from_user.id, pack=pack
                )
            except DraftNotEmptyError:
                await state.clear()
                await callback.answer(
                    await render_template(db, channel_id, "custom.transfer_draft_exists"),
                    show_alert=True,
                ); return
            except TelegramAPIError:
                await state.clear()
                await callback.answer(
                    await render_template(db, channel_id, "custom.transfer_media_unavailable"),
                    show_alert=True,
                ); return
            except (CustomPackValidationError, ValueError) as exc:
                await state.clear()
                error = exc.message if isinstance(exc, CustomPackValidationError) else str(exc)
                # Callback-query alerts are capped at a short Bot API text limit.
                # Validation errors can include a template key and precise reason,
                # so deliver the full diagnostic as a normal private message.
                await callback.message.answer(
                    await render_template(db, channel_id, "custom.transfer_invalid", error=error)
                )
                await callback.answer(); return
            except PermissionError:
                await state.clear()
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
            await state.clear()
            await callback.message.edit_text(
                await render_template(
                    db, channel_id, "custom.transfer_staged",
                    count=int(result.get("staged") or 0),
                    skipped=int(result.get("skipped") or 0),
                ),
                reply_markup=await custom_transfer_staged_keyboard(db=db, channel_id=channel_id),
            )
            await callback.answer(); return

        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)

    # --------------------------------------------------------------
    # Channel Custom Pack bulk tools: reset initial / current Standard / copy own channel
    # --------------------------------------------------------------
    @router.callback_query(F.data.startswith("custom:tools:"))
    async def custom_tools_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 4:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        action = parts[2]
        try:
            channel_id = int(parts[3])
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return

        if action == "home":
            text, keyboard = await custom_tools_view(db=db, channel=decision.channel)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer(); return

        if action in {"reset_initial", "apply_standard", "copy", "copy_source", "reset_initial_confirm", "apply_standard_confirm", "copy_confirm"}:
            if await db.has_channel_custom_draft(channel_id):
                await callback.answer(await render_template(db, channel_id, "custom.tools_draft_exists"), show_alert=True); return

        if action == "reset_initial":
            try:
                plan = await db.plan_channel_custom_initial_reset(channel_id=channel_id)
            except ValueError:
                await callback.answer(await render_template(db, channel_id, "custom.history_unavailable"), show_alert=True); return
            if int(plan.get("staged") or 0) == 0:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            text = await custom_tools_plan_text(
                db=db, channel_id=channel_id, title="Вернуть исходный кастом", plan=plan
            )
            text += "\n\n" + await render_template(db, channel_id, "custom.tools_reset_initial_prompt")
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data=f"custom:tools:reset_initial_confirm:{channel_id}")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"custom:tools:home:{channel_id}")],
            ]))
            await callback.answer(); return

        if action == "reset_initial_confirm":
            try:
                result = await db.stage_channel_custom_initial_reset(
                    channel_id=channel_id, reset_by=callback.from_user.id
                )
            except DraftNotEmptyError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_draft_exists"), show_alert=True); return
            except ValueError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            await callback.message.edit_text(
                await render_template(
                    db, channel_id, "custom.tools_staged", title="Исходный кастом",
                    count=int(result["staged"]), skipped=int(result["skipped"]),
                ),
                reply_markup=await custom_tools_staged_keyboard(db=db, channel_id=channel_id),
            )
            await callback.answer(); return

        if action == "apply_standard":
            try:
                plan = await db.plan_channel_custom_apply_current_standard(channel_id=channel_id)
            except ValueError:
                await callback.answer(await render_template(db, channel_id, "custom.history_unavailable"), show_alert=True); return
            if int(plan.get("staged") or 0) == 0:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            standard_revision_id = int(plan["source_standard_revision_id"])
            text = await custom_tools_plan_text(
                db=db, channel_id=channel_id, title=f"Актуальный стандарт №{standard_revision_id}", plan=plan
            )
            text += "\n\n" + await render_template(
                db, channel_id, "custom.tools_apply_standard_prompt",
                standard_revision_id=standard_revision_id,
            )
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data=f"custom:tools:apply_standard_confirm:{channel_id}")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"custom:tools:home:{channel_id}")],
            ]))
            await callback.answer(); return

        if action == "apply_standard_confirm":
            try:
                result = await db.stage_channel_custom_current_standard(
                    channel_id=channel_id, applied_by=callback.from_user.id
                )
            except DraftNotEmptyError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_draft_exists"), show_alert=True); return
            except ValueError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            await callback.message.edit_text(
                await render_template(
                    db, channel_id, "custom.tools_staged", title="Актуальный стандарт",
                    count=int(result["staged"]), skipped=int(result["skipped"]),
                ),
                reply_markup=await custom_tools_staged_keyboard(db=db, channel_id=channel_id),
            )
            await callback.answer(); return

        if action == "copy":
            channels = [
                row for row in await db.list_enabled_channels_for_owner(callback.from_user.id)
                if int(row["channel_id"]) != channel_id
            ]
            if not channels:
                await callback.answer(await render_template(db, channel_id, "custom.tools_copy_none"), show_alert=True); return
            rows = [[InlineKeyboardButton(
                text=str(row["group_title"])[:60],
                callback_data=f"custom:tools:copy_source:{channel_id}:{int(row['channel_id'])}",
            )] for row in channels]
            rows.append([InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.back"), callback_data=f"custom:tools:home:{channel_id}")])
            await callback.message.edit_text(
                await render_template(db, channel_id, "custom.tools_copy_choose"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            await callback.answer(); return

        if action == "copy_source":
            if len(parts) != 5:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
            try:
                source_channel_id = int(parts[4])
                plan = await db.plan_channel_custom_copy(
                    channel_id=channel_id, source_channel_id=source_channel_id,
                    actor_id=callback.from_user.id,
                )
            except (ValueError, PermissionError):
                await callback.answer(await render_template(db, channel_id, "custom.tools_copy_unavailable"), show_alert=True); return
            if int(plan.get("staged") or 0) == 0:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            source_name = str(plan["source_channel_name"])
            text = await custom_tools_plan_text(
                db=db, channel_id=channel_id, title=f"Копирование из «{source_name}»", plan=plan
            )
            text += "\n\n" + await render_template(
                db, channel_id, "custom.tools_copy_prompt",
                source_name=source_name, target_name=str(decision.channel["group_title"]),
            )
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data=f"custom:tools:copy_confirm:{channel_id}:{source_channel_id}")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data=f"custom:tools:home:{channel_id}")],
            ]))
            await callback.answer(); return

        if action == "copy_confirm":
            if len(parts) != 5:
                await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
            try:
                source_channel_id = int(parts[4])
                result = await db.stage_channel_custom_copy(
                    channel_id=channel_id, source_channel_id=source_channel_id,
                    copied_by=callback.from_user.id,
                )
            except DraftNotEmptyError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_draft_exists"), show_alert=True); return
            except PermissionError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_copy_unavailable"), show_alert=True); return
            except ValueError:
                await callback.answer(await render_template(db, channel_id, "custom.tools_no_changes"), show_alert=True); return
            await callback.message.edit_text(
                await render_template(
                    db, channel_id, "custom.tools_staged", title="Скопированное оформление",
                    count=int(result["staged"]), skipped=int(result["skipped"]),
                ),
                reply_markup=await custom_tools_staged_keyboard(db=db, channel_id=channel_id),
            )
            await callback.answer(); return

        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)

    # Channel Custom Pack revision history / audit / rollback-to-draft
    # --------------------------------------------------------------
    @router.callback_query(F.data.startswith("custom:history:"))
    async def custom_history_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 4:
                raise ValueError
            channel_id, page = int(parts[2]), max(0, int(parts[3]))
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        text, keyboard = await custom_history_view(db=db, channel=decision.channel, page=page)
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:revision:"))
    async def custom_revision_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 5:
                raise ValueError
            channel_id, revision_id, page = int(parts[2]), int(parts[3]), max(0, int(parts[4]))
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        view = await custom_revision_view(
            db=db, channel=decision.channel, revision_id=revision_id, history_page=page
        )
        if view is None:
            await callback.answer(await render_template(db, channel_id, "custom.history_unavailable"), show_alert=True); return
        text, keyboard = view
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:revision_preview:"))
    async def custom_revision_preview_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 5:
                raise ValueError
            channel_id, revision_id = int(parts[2]), int(parts[3])
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        revision = await db.get_channel_custom_revision(channel_id=channel_id, revision_id=revision_id)
        if revision is None:
            await callback.answer(await render_template(db, channel_id, "custom.history_unavailable"), show_alert=True); return
        media_ok = await send_channel_start_card_preview(
            message=callback.message, db=db, channel=decision.channel, revision_id=revision_id
        )
        if not media_ok:
            await callback.answer(await render_template(db, channel_id, "start_card.media_stale"), show_alert=True); return
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:revision_restore:"))
    async def custom_revision_restore_prompt(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 5:
                raise ValueError
            channel_id, revision_id, page = int(parts[2]), int(parts[3]), max(0, int(parts[4]))
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        revision = await db.get_channel_custom_revision(channel_id=channel_id, revision_id=revision_id)
        state_row = await db.get_channel_custom_state(channel_id)
        if revision is None or state_row is None:
            await callback.answer(await render_template(db, channel_id, "custom.history_unavailable"), show_alert=True); return
        if int(state_row["active_revision_id"]) == revision_id:
            await callback.answer(await render_template(db, channel_id, "custom.history_restore_current"), show_alert=True); return
        if await db.has_channel_custom_draft(channel_id):
            await callback.answer(await render_template(db, channel_id, "custom.history_draft_exists"), show_alert=True); return
        await callback.message.answer(
            await render_template(db, channel_id, "custom.history_restore_prompt", revision_id=revision_id),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.common.confirm"),
                    callback_data=f"custom:revision_restore_confirm:{channel_id}:{revision_id}:{page}",
                )],
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.common.cancel"),
                    callback_data=f"custom:revision:{channel_id}:{revision_id}:{page}",
                )],
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:revision_restore_confirm:"))
    async def custom_revision_restore_confirm(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 5:
                raise ValueError
            channel_id, revision_id, page = int(parts[2]), int(parts[3]), max(0, int(parts[4]))
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        try:
            result = await db.stage_channel_custom_revision_restore(
                channel_id=channel_id, revision_id=revision_id, restored_by=callback.from_user.id
            )
        except DraftNotEmptyError:
            await callback.answer(await render_template(db, channel_id, "custom.history_draft_exists"), show_alert=True); return
        except ValueError:
            state_row = await db.get_channel_custom_state(channel_id)
            if state_row is not None and int(state_row["active_revision_id"]) == revision_id:
                key = "custom.history_restore_current"
            else:
                key = "custom.history_unavailable"
            await callback.answer(await render_template(db, channel_id, key), show_alert=True); return
        await callback.message.answer(
            await render_template(
                db, channel_id, "custom.history_restore_staged",
                revision_id=revision_id, count=int(result["staged"]), skipped=int(result["skipped"]),
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.common.preview"),
                    callback_data=f"custom:draft_preview:{channel_id}",
                )],
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.custom.publish"),
                    callback_data=f"custom:publish:{channel_id}",
                )],
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.custom.discard"),
                    callback_data=f"custom:discard:{channel_id}",
                )],
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.common.back"),
                    callback_data=f"custom:revision:{channel_id}:{revision_id}:{page}",
                )],
            ]),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:draft_preview:"))
    async def custom_draft_preview_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        try:
            channel_id = int((callback.data or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        if not await db.has_channel_custom_draft(channel_id):
            await callback.answer(await render_template(db, channel_id, "custom.publish_empty"), show_alert=True); return
        media_ok = await send_channel_start_card_preview(
            message=callback.message, db=db, channel=decision.channel
        )
        if not media_ok:
            await callback.answer(await render_template(db, channel_id, "start_card.media_stale"), show_alert=True); return
        await callback.answer()

    @router.callback_query(F.data.startswith("preview:subscriber:"))
    async def subscriber_preview_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        if callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer(
                "Предпросмотр доступен только в личном чате. Откройте /panel у бота.",
                show_alert=True,
            )
            return
        parts = (callback.data or "").split(":")
        scenario_key: str | None = None
        try:
            if len(parts) == 4 and parts[:2] == ["preview", "subscriber"]:
                action = parts[2]
                channel_id = int(parts[3])
                if action not in {"home", "all", "noop"}:
                    raise ValueError
            elif len(parts) == 5 and parts[:3] == ["preview", "subscriber", "scenario"]:
                action = "scenario"
                scenario_key = parts[3]
                channel_id = int(parts[4])
                if scenario_key not in SUBSCRIBER_PREVIEW_BY_KEY:
                    raise ValueError
            else:
                raise ValueError
        except (TypeError, ValueError):
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        decision = await authorizer.require(
            actor_id=callback.from_user.id,
            channel_id=channel_id,
            action=ChannelAction.SETTINGS,
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        if action == "home":
            text, keyboard = await subscriber_preview_home(db=db, channel=decision.channel)
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return
        if action == "noop":
            await callback.answer(
                "Предпросмотр: выбор не сохраняется и реальную приватность не меняет.",
                show_alert=True,
            )
            return
        if action == "all":
            media_ok = await send_all_subscriber_preview_scenarios(
                message=callback.message, db=db, channel=decision.channel
            )
        else:
            media_ok = await send_subscriber_preview_scenario(
                message=callback.message, db=db, channel=decision.channel,
                scenario_key=str(scenario_key),
            )
        if not media_ok:
            await callback.answer(
                await render_template(db, channel_id, "start_card.media_stale"),
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("custom:audit:"))
    async def custom_audit_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) != 4:
                raise ValueError
            channel_id, page = int(parts[2]), max(0, int(parts[3]))
        except ValueError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        decision = await authorizer.require(
            actor_id=callback.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True); return
        text, keyboard = await custom_audit_view(db=db, channel=decision.channel, page=page)
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    @router.message(CustomTransferFlow.import_file, F.chat.type == ChatType.PRIVATE)
    async def custom_transfer_import_file(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        try:
            channel_id = int(data.get("import_channel_id") or 0)
        except (TypeError, ValueError):
            await state.clear(); return
        decision = await authorizer.require(
            actor_id=message.from_user.id, channel_id=channel_id, action=ChannelAction.SETTINGS
        )
        if not decision.allowed or decision.channel is None:
            await state.clear()
            await message.answer(ACCESS_DENIED_TEXT); return
        if await db.has_channel_custom_draft(channel_id):
            await state.clear()
            await message.answer(await render_template(db, channel_id, "custom.transfer_draft_exists")); return
        if message.document is None:
            await message.answer(
                await render_template(db, channel_id, "custom.transfer_invalid", error="отправьте файл как документ JSON."),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, channel_id, "ui.custom.cancel_import"),
                        callback_data=f"custom:transfer:cancel_import:{channel_id}",
                    )
                ]]),
            ); return
        if message.document.file_size is not None and int(message.document.file_size) > MAX_CUSTOM_PACK_BYTES:
            await message.answer(
                await render_template(
                    db, channel_id, "custom.transfer_invalid",
                    error=f"файл слишком большой; максимум {MAX_CUSTOM_PACK_BYTES // 1024} КБ.",
                )
            ); return
        try:
            buffer = io.BytesIO()
            await bot.download(message.document, destination=buffer)
            raw = buffer.getvalue()
            document, digest = parse_import_bytes(raw)
            pack = normalize_import_document(document, raw_sha256=digest)
        except CustomPackValidationError as exc:
            await message.answer(
                await render_template(db, channel_id, "custom.transfer_invalid", error=exc.message),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, channel_id, "ui.custom.cancel_import"),
                        callback_data=f"custom:transfer:cancel_import:{channel_id}",
                    )
                ]]),
            ); return
        except TelegramAPIError:
            await message.answer(
                await render_template(db, channel_id, "custom.transfer_invalid", error="Telegram не смог скачать этот документ. Отправьте файл повторно.")
            ); return

        if pack.media_file_id is not None:
            try:
                await bot.get_file(pack.media_file_id)
            except TelegramAPIError:
                await message.answer(
                    await render_template(db, channel_id, "custom.transfer_media_unavailable"),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text=await render_label(db, channel_id, "ui.custom.cancel_import"),
                            callback_data=f"custom:transfer:cancel_import:{channel_id}",
                        )
                    ]]),
                ); return
        try:
            plan = await db.plan_channel_custom_import(
                channel_id=channel_id, actor_id=message.from_user.id, pack=pack
            )
        except PermissionError:
            await state.clear()
            await message.answer(ACCESS_DENIED_TEXT); return
        except ValueError as exc:
            await state.clear()
            await message.answer(
                await render_template(db, channel_id, "custom.transfer_invalid", error=str(exc))
            ); return
        if int(plan.get("staged") or 0) == 0:
            await state.clear()
            await message.answer(
                await render_template(db, channel_id, "custom.transfer_no_changes"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, channel_id, "ui.common.back"),
                        callback_data=f"custom:transfer:home:{channel_id}",
                    )
                ]]),
            ); return

        await state.set_state(CustomTransferFlow.import_confirmation)
        await state.update_data(
            import_channel_id=channel_id,
            import_document=document,
            import_sha256=digest,
        )
        await message.answer(
            await custom_transfer_plan_text(db=db, channel_id=channel_id, plan=plan),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.custom.confirm_import"),
                    callback_data=f"custom:transfer:confirm_import:{channel_id}",
                )],
                [InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.custom.cancel_import"),
                    callback_data=f"custom:transfer:cancel_import:{channel_id}",
                )],
            ]),
        )

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
        await send_channel_start_card(message=message, db=db, channel=channel)
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
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.privacy.anonymous"), callback_data="privacy:anonymous"),
            InlineKeyboardButton(text=await render_label(db, channel_id, "ui.privacy.identified"), callback_data="privacy:identified"),
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
                duration = await channel_sanction_notice_duration(db, channel_id, action, until)
                await message.answer(await render_template(
                    db, channel_id, f"sanction.active.{'visible' if show_reason else 'hidden'}",
                    action=await channel_sanction_action_label(db, channel_id, action), duration=duration,
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
        channel_id = int(topic["channel_id"])
        lines = [await render_template(db, channel_id, "subscriber.history_title")]
        for row in rows[:20]:
            action = await channel_sanction_action_label(db, channel_id, str(row["action"]))
            reason = (
                await render_template(db, channel_id, "subscriber.history.simple_reason_suffix", reason=str(row["reason"]))
                if row["reason"] else ""
            )
            lines.append(await render_template(
                db, channel_id, "subscriber.history.simple_entry",
                created_at=str(row["created_at"])[:16], action=action, reason=reason,
            ))
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
        channel_id = int(topic["channel_id"]); user_id = int(topic["user_id"])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.rate_limit"), callback_data=f"subscriber:rate:{channel_id}:{user_id}"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.mute"), callback_data=f"subscriber:action:{channel_id}:{user_id}:mute")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.temporary_block"), callback_data=f"subscriber:action:{channel_id}:{user_id}:temporary_block"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.permanent_block"), callback_data=f"subscriber:action:{channel_id}:{user_id}:permanent_block")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.warning"), callback_data=f"subscriber:action:{channel_id}:{user_id}:warning"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.clear"), callback_data=f"subscriber:clear:{channel_id}:{user_id}")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.add_note"), callback_data=f"subscriber:meta:add:note:{channel_id}:{user_id}"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.add_tag"), callback_data=f"subscriber:meta:add:tag:{channel_id}:{user_id}")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.notes"), callback_data="subscriber:meta:view:notes:0"), InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.tags"), callback_data="subscriber:meta:view:tags:0")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.statistics"), callback_data="subscriber:stats")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.subscriber.history"), callback_data="subscriber:history:0")],
        ])
        await message.answer(await render_template(db, int(topic["channel_id"]), "subscriber.actions_prompt"), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("subscriber:history:"))
    async def subscriber_restriction_history(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        detail = len(parts) == 5 and parts[2] == "detail"
        try:
            page = int(parts[4] if detail else parts[2])
            item_id = int(parts[3]) if detail else None
        except (IndexError, ValueError):
            await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True)
            return
        context = await metadata_context(callback.message)
        if context is None or page < 0:
            await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True)
            return
        topic, target = context
        cid, uid = int(topic["channel_id"]), int(topic["user_id"])
        channel = await db.get_channel_by_id(cid)
        if channel is None or not bool(channel["enabled"]):
            await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True)
            return
        total = await db.count_subscriber_moderation_history(channel_id=cid, user_id=uid)
        page = min(page, max(0, (total - 1) // 8))
        rows = await db.get_subscriber_moderation_history(
            channel_id=cid, user_id=uid, offset=page * 8, limit=8
        )

        async def action_label(action: object) -> str:
            key = f"ui.sanction.action.{str(action)}"
            return await render_label(db, cid, key) if key in TEMPLATE_REGISTRY else str(action)

        async def status_label(status: object) -> str:
            key = f"ui.sanction.status.{str(status)}"
            return await render_label(db, cid, key) if key in TEMPLATE_REGISTRY else str(status)

        if detail:
            row = next((row for row in rows if int(row["item_id"]) == item_id), None)
            if row is None:
                await callback.answer(await callback_metadata_text(callback, "not_found"), show_alert=True)
                return
            admin = await render_label(
                db, cid, "ui.sanction.admin" if row.get("admin_id") is not None else "ui.sanction.system"
            )
            expires = (
                dt_from_db(str(row["expires_at"]))
                .astimezone(ZoneInfo(str(channel["timezone"])))
                .strftime("%d.%m.%Y %H:%M")
                if row["expires_at"] else "—"
            )
            text = await render_template(
                db, cid, "subscriber.history.detail",
                action=await action_label(row["action"]),
                target=target,
                status=await status_label(row["status"]),
                admin=admin,
                reason=str(row["reason"] or "—"),
                show_reason=await render_label(
                    db, cid, "ui.common.yes" if row["show_reason_to_subscriber"] else "ui.common.no"
                ),
                expires_at=expires,
            )
            await callback.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=await render_label(db, cid, "ui.common.back"),
                        callback_data=f"subscriber:history:{page}",
                    )
                ]]),
            )
            await callback.answer()
            return

        zone = ZoneInfo(str(channel["timezone"]))
        lines = [await render_template(
            db, cid, "subscriber.history.page",
            target=target, page=page + 1, pages=max(1, (total + 7) // 8),
        )]
        for row in rows:
            when = dt_from_db(str(row["created_at"])).astimezone(zone).strftime("%d.%m.%Y %H:%M")
            reason = (
                await render_template(db, cid, "subscriber.history.reason_suffix", reason=str(row["reason"]))
                if row["reason"] else ""
            )
            lines.append(await render_template(
                db, cid, "subscriber.history.entry",
                action=await action_label(row["action"]),
                created_at=when,
                status=await status_label(row["status"]),
                reason=reason,
            ))
        if not rows:
            lines.append(await render_template(db, cid, "subscriber.history.no_rows"))
        nav = []
        event_buttons = [[InlineKeyboardButton(
            text=await render_label(db, cid, "ui.subscriber.details", position=index + 1),
            callback_data=f"subscriber:history:detail:{int(row['item_id'])}:{page}",
        )] for index, row in enumerate(rows)]
        if page:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"subscriber:history:{page - 1}"))
        if (page + 1) * 8 < total:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"subscriber:history:{page + 1}"))
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=event_buttons + ([nav] if nav else [])
            ) if event_buttons or nav else None,
        )
        await callback.answer()

    @router.callback_query(F.data == "subscriber:stats")
    async def subscriber_statistics(callback: CallbackQuery) -> None:
        if callback.message is None:
            return
        context = await metadata_context(callback.message)
        if context is None:
            await callback.answer(await callback_metadata_text(callback, "access_denied"), show_alert=True)
            return
        topic, target = context
        cid = int(topic["channel_id"])
        channel = await db.get_channel_by_id(cid)
        if channel is None or not bool(channel["enabled"]):
            await callback.answer(await callback_metadata_text(callback, "expired"), show_alert=True)
            return
        stats = await db.get_subscriber_statistics(
            channel_id=cid, user_id=int(topic["user_id"]), timezone_name=str(channel["timezone"])
        )

        def duration(value: object) -> str:
            return "—" if value is None else f"{round(float(value) / 60, 1)} мин"

        media = stats["media"]
        moderation = stats["moderation"]
        text = await render_template(
            db, cid, "subscriber.statistics",
            target=target,
            subscriber_messages=stats["subscriber_messages"],
            admin_replies=stats["admin_replies"],
            active_days=stats["active_days"],
            last_7_days=stats["last_7_days"],
            last_30_days=stats["last_30_days"],
            conversations=stats["conversations"],
            answered_conversations=stats["answered_conversations"],
            answered_percentage=stats["answered_percentage"],
            closed_conversations=stats["closed_conversations"],
            average_messages_per_conversation=stats["average_messages_per_conversation"],
            average_first_response=duration(stats["average_first_response_seconds"]),
            median_first_response=duration(stats["median_first_response_seconds"]),
            text_count=media["text"], photo_count=media["photo"], video_count=media["video"],
            document_count=media["document"], voice_count=media["voice"], audio_count=media["audio"],
            sticker_count=media["sticker"], other_count=media["other"],
            warnings=moderation["warnings"], restrictions=moderation["restrictions"],
            active_restrictions=moderation["active_restrictions"], notes=moderation["notes"], tags=moderation["tags"],
        )
        await callback.message.answer(text)
        await callback.answer()

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
        channel_id = int(topic["channel_id"])
        if str(topic["privacy_mode"]) == "anonymous":
            tag = await db.get_anonymous_tag(
                channel_id=channel_id, user_id=int(topic["user_id"]),
            )
            target = await render_template(
                db, channel_id, "subscriber.metadata.target_anonymous",
                anonymous_tag=tag or "Анон",
            )
            return topic, target
        target = await render_template(
            db, channel_id, "subscriber.metadata.target_identified",
            user_id=int(topic["user_id"]),
        )
        return topic, target

    async def metadata_context_from_state(message: Message, state: FSMContext) -> tuple[object, str] | None:
        context = await metadata_context(message)
        data = await state.get_data()
        if context is None:
            return None
        topic, target = context
        if (data.get("channel_id"), data.get("target_user_id")) != (int(topic["channel_id"]), int(topic["user_id"])):
            return None
        return topic, target

    async def metadata_cancel_keyboard(channel_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=await render_label(db, channel_id, "ui.common.cancel"),
                callback_data="subscriber:meta:cancel",
            )
        ]])

    async def metadata_page_keyboard(*, channel_id: int, kind: str, page: int, total: int, rows: list[object]) -> InlineKeyboardMarkup:
        buttons: list[list[InlineKeyboardButton]] = []
        if kind == "notes":
            for index, row in enumerate(rows):
                buttons.append([InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.metadata.open_note", position=index + 1),
                    callback_data=f"subscriber:meta:note:{int(row['note_id'])}:open:{page}",
                )])
        else:
            for index, row in enumerate(rows):
                buttons.append([InlineKeyboardButton(
                    text=await render_label(db, channel_id, "ui.metadata.delete_tag", position=index + 1),
                    callback_data=f"subscriber:meta:tag:{int(row['tag_id'])}:delete:{page}",
                )])
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
        await callback.message.answer(prompt, reply_markup=await metadata_cancel_keyboard(channel_id))
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
        await callback.message.answer(title, reply_markup=await metadata_page_keyboard(channel_id=channel_id,kind=kind,page=page,total=total,rows=rows))
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
                [
                    InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.edit"),callback_data=f"subscriber:meta:note:{note_id}:edit:{page}"),
                    InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.delete"),callback_data=f"subscriber:meta:note:{note_id}:delete:{page}"),
                ],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.metadata.to_notes"),callback_data=f"subscriber:meta:view:notes:{page}")],
            ])
            await callback.message.answer(rendered,reply_markup=keyboard); await callback.answer(); return
        await state.clear(); await state.update_data(channel_id=channel_id,target_user_id=user_id,privacy_mode=str(topic["privacy_mode"]),note_id=note_id,page=page)
        if action == "edit":
            await state.set_state(SubscriberMetadataFlow.note_edit)
            await callback.message.answer(await callback_metadata_text(callback, "edit_prompt"),reply_markup=await metadata_cancel_keyboard(channel_id))
        else:
            await state.set_state(SubscriberMetadataFlow.note_delete_confirmation)
            await callback.message.answer(await metadata_text(channel_id, "delete_note_confirmation", text=str(note["note_text"])),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.delete"),callback_data="subscriber:meta:notedelete:apply")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"),callback_data="subscriber:meta:cancel")],
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
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.save"),callback_data="subscriber:meta:noteedit:apply")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"),callback_data="subscriber:meta:cancel")],
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
        channel_id = int(topic["channel_id"])
        await callback.message.answer(await metadata_text(channel_id, "delete_tag_confirmation", tag=str(tag["tag"])),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.delete"),callback_data="subscriber:meta:tagdelete:apply")],
            [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"),callback_data="subscriber:meta:cancel")],
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
                reply_markup=await sanction_duration_keyboard(db, channel_id, action),
            )
        else:
            await state.set_state(SanctionFlow.reason)
            await callback.message.answer(await callback_flow_text(callback, "choose_reason"),reply_markup=await sanction_reason_keyboard(db, channel_id))
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
        keyboard = await sanction_duration_keyboard(db, channel_id, "rate_limit")
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
                reply_markup=await sanction_reason_keyboard(db, int(data["channel_id"])),
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
            reply_markup=await sanction_reason_keyboard(db, int(data["channel_id"])),
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
                reply_markup=await sanction_visibility_keyboard(db, int(data["channel_id"])),
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
            reply_markup=await sanction_visibility_keyboard(db, int(data["channel_id"])),
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
            channel_id = int(data["channel_id"])
            values = await sanction_confirmation_values_channel(
                db, channel_id, data, anonymous_tag=anonymous_tag
            )
            await callback.message.answer(
                await render_template(db, channel_id, "sanction.flow.confirmation", **values),
                reply_markup=await sanction_confirmation_keyboard(db, channel_id),
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
            confirmation_values = await sanction_confirmation_values_channel(
                db, int(data["channel_id"]), data, anonymous_tag=tag
            )
            confirmation = await render_template(
                db, int(data["channel_id"]), "sanction.flow.confirmation",
                **confirmation_values,
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
        labels = ", ".join([
            await channel_sanction_action_label(db, channel_id, str(row["action"]))
            for row in active
        ])
        await callback.message.answer(
            await callback_flow_text(callback, "clear_confirmation", actions=labels),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.confirm"), callback_data="sanction:clear_confirm")],
                [InlineKeyboardButton(text=await render_label(db, channel_id, "ui.common.cancel"), callback_data="sanction:cancel")],
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

    STATUS_LABEL_KEYS = {
        "new": "ui.status.new",
        "in_progress": "ui.status.in_progress",
        "answered": "ui.status.answered",
        "closed": "ui.status.closed",
    }

    async def status_label(channel_id: int, status: str) -> str:
        key = STATUS_LABEL_KEYS.get(status)
        return await render_label(db, channel_id, key) if key else status

    async def topic_status_keyboard(topic) -> InlineKeyboardMarkup:
        current = str(topic["status"])
        channel_id = int(topic["channel_id"])
        labels = {key: await status_label(channel_id, key) for key in STATUS_LABEL_KEYS}
        rows = [
            [
                InlineKeyboardButton(text=("✓ " if current == "new" else "") + labels["new"], callback_data="topic:status:new"),
                InlineKeyboardButton(text=("✓ " if current == "in_progress" else "") + labels["in_progress"], callback_data="topic:status:in_progress"),
            ],
            [
                InlineKeyboardButton(text=("✓ " if current == "answered" else "") + labels["answered"], callback_data="topic:status:answered"),
                InlineKeyboardButton(text=("✓ " if current == "closed" else "") + labels["closed"], callback_data="topic:status:closed"),
            ],
            [
                InlineKeyboardButton(
                    text=await render_label(
                        db, channel_id,
                        "ui.status.unmark_important" if bool(topic["is_important"]) else "ui.status.mark_important",
                    ),
                    callback_data="topic:protect:important:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=await render_label(
                        db, channel_id,
                        "ui.status.unprotect" if bool(topic["is_pinned"]) else "ui.status.protect",
                    ),
                    callback_data="topic:protect:pinned:toggle",
                )
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def topic_status_text(topic) -> str:
        channel_id = int(topic["channel_id"])
        return await render_template(
            db,
            channel_id,
            "status.overview",
            status=await status_label(channel_id, str(topic["status"])),
            important=await render_label(db, channel_id, "ui.common.yes" if bool(topic["is_important"]) else "ui.common.no"),
            pinned=await render_label(db, channel_id, "ui.common.yes" if bool(topic["is_pinned"]) else "ui.common.no"),
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
            await message.answer(await topic_status_text(topic), reply_markup=await topic_status_keyboard(topic))
            return
        aliases = {"new": "new", "новый": "new", "новое": "new", "in_progress": "in_progress", "в_работе": "in_progress", "answered": "answered", "отвечен": "answered", "отвечено": "answered", "closed": "closed", "закрыт": "closed", "закрыто": "closed"}
        status = aliases.get(value)
        if status is None:
            await message.answer(await render_template(db, int(topic["channel_id"]), "status.usage"), reply_markup=await topic_status_keyboard(topic))
            return
        await db.set_topic_status(channel_id=int(topic["channel_id"]), user_id=int(topic["user_id"]), privacy_mode=str(topic["privacy_mode"]), status=status)
        updated = await db.get_topic_by_group_thread(group_id=message.chat.id, topic_id=message.message_thread_id)
        await message.answer(
            await render_template(db, int(topic["channel_id"]), "status.changed", status=await status_label(int(topic["channel_id"]), status)),
            reply_markup=await topic_status_keyboard(updated or topic),
        )

    @router.callback_query(F.data.startswith("topic:status:"))
    async def topic_status_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.message.message_thread_id:
            await callback.answer(render_default("status.unavailable", {}), show_alert=True)
            return
        status = (callback.data or "").rsplit(":", 1)[-1]
        if status not in STATUS_LABEL_KEYS:
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
            await callback.answer(await render_template(db, int(topic["channel_id"]), "status.unavailable"), show_alert=True)
            return
        await callback.message.edit_text(await topic_status_text(updated), reply_markup=await topic_status_keyboard(updated))
        await callback.answer(await render_template(db, int(topic["channel_id"]), "status.changed", status=await status_label(int(topic["channel_id"]), status)))

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
            await callback.answer(await render_template(db, int(topic["channel_id"]), "status.unavailable"), show_alert=True)
            return
        await callback.message.edit_text(await topic_status_text(updated), reply_markup=await topic_status_keyboard(updated))
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
            await callback.answer(await render_template(db, channel_id, "broadcast.owner_required"), show_alert=True)
            return None, None
        broadcast = await db.get_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
        if broadcast is None or int(broadcast["created_by"]) != callback.from_user.id:
            await callback.answer(await render_template(db, channel_id, "broadcast.unavailable"), show_alert=True)
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
            await message.answer(await render_template(db, int(channel["channel_id"]), "broadcast.owner_required"))
            return
        channel_id = int(channel["channel_id"])
        active = await db.get_sending_broadcast(channel_id=channel_id)
        await state.clear()
        if active is not None:
            broadcast_id = str(active["broadcast_id"])
            await message.answer(
                await render_template(db, channel_id, "broadcast.resume_available"),
                reply_markup=await broadcast_resume_keyboard(db, channel_id, broadcast_id),
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
            await message.answer(await render_template(db, channel_id, "broadcast.unavailable"))
            return
        if message.chat.id != group_id or not is_general_forum_message(message):
            await message.answer(await render_template(db, channel_id, "broadcast.general_required"))
            return
        if message.from_user is None or message.from_user.id != owner_id:
            await state.clear()
            await message.answer(await render_template(db, channel_id, "broadcast.owner_required"))
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
            await message.answer(await render_template(db, channel_id, "broadcast.owner_required"))
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
            await message.answer(await render_template(db, channel_id, "broadcast.unavailable"))
            return
        await message.answer(
            await render_template(db, channel_id, "broadcast.preview_ready"),
            reply_markup=await broadcast_preview_keyboard(db, channel_id, broadcast_id),
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
                await callback.answer(await render_template(db, channel_id, "broadcast.unavailable"), show_alert=True)
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
                await callback.answer(await render_template(db, channel_id, "broadcast.unavailable"), show_alert=True)
                return
            await state.clear()
            await callback.message.edit_text(await render_template(db, channel_id, "broadcast.cancelled"))
            await callback.answer()
            return

        if action == "send":
            if status != "draft":
                await callback.answer(await render_template(db, channel_id, "broadcast.unavailable"), show_alert=True)
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
                    await render_template(db, channel_id, text),
                    show_alert=True,
                )
                return
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            await callback.message.answer(await render_template(db, channel_id, "broadcast.started"))
        else:  # resume
            if status != "sending":
                await callback.answer(await render_template(db, channel_id, "broadcast.unavailable"), show_alert=True)
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
