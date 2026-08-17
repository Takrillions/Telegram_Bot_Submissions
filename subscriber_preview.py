"""Read-only owner preview of the subscriber-facing channel experience.

The module intentionally contains no database mutations and no Telegram API
calls.  Handlers use it to render draft-aware sample messages, while the
preview renderer stays separate from runtime mutation paths.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from templates import render_template


@dataclass(frozen=True, slots=True)
class SubscriberPreviewScenario:
    key: str
    title: str
    template_key: str | None = None
    sample_values: tuple[tuple[str, str], ...] = ()

    def values(self) -> dict[str, str]:
        return dict(self.sample_values)


SUBSCRIBER_PREVIEW_SCENARIOS: tuple[SubscriberPreviewScenario, ...] = (
    SubscriberPreviewScenario("start", "Стартовая карточка"),
    SubscriberPreviewScenario("privacy", "Выбор приватности", "privacy.prompt"),
    SubscriberPreviewScenario("received", "Сообщение получено", "message.received"),
    SubscriberPreviewScenario(
        "unavailable", "Предложка недоступна", "message.channel_unavailable"
    ),
    SubscriberPreviewScenario(
        "sanction",
        "Пример ограничения",
        "sanction.applied.visible",
        (
            ("action", "Ограничение"),
            ("duration", "до 18.08.2026 14:30"),
            ("reason", "Нарушение правил сообщества"),
        ),
    ),
    # Real admin replies are copied verbatim from the forum topic and therefore
    # have no channel template.  The owner preview shows a representative copy.
    SubscriberPreviewScenario("admin_reply", "Ответ администратора"),
    SubscriberPreviewScenario("cleanup", "Предупреждение об очистке"),
)

SUBSCRIBER_PREVIEW_BY_KEY = {
    scenario.key: scenario for scenario in SUBSCRIBER_PREVIEW_SCENARIOS
}

SAMPLE_ADMIN_REPLY = (
    "Здравствуйте. Спасибо за обращение. Администратор ознакомился с сообщением "
    "и передал информацию ответственному сотруднику."
)


def customization_context_header(
    *,
    channel_name: str,
    channel_id: int,
    active_revision_id: int | None,
    draft_count: int,
) -> str:
    """Stable owner-only header that cannot be hidden by channel custom text."""
    safe_name = html.escape(channel_name)
    if active_revision_id is None:
        revision = "недоступна"
    else:
        revision = f"№{active_revision_id}"
    if draft_count > 0:
        status = (
            f"черновик: <b>{draft_count}</b> изм. поверх опубликованной версии {revision}"
        )
    else:
        status = f"опубликованная версия {revision}; черновика нет"
    return (
        f"<b>Сейчас редактируется:</b> {safe_name} · <code>channel_id={channel_id}</code>\n"
        f"<b>Состояние оформления:</b> {status}"
    )


def subscriber_preview_marker(
    *,
    channel_name: str,
    channel_id: int,
    scenario_title: str,
    draft_count: int,
) -> str:
    source = (
        f"черновик + опубликованная версия ({draft_count} изм.)"
        if draft_count > 0
        else "опубликованная версия"
    )
    return (
        "<blockquote><b>ПРЕДПРОСМОТР — ничего не сохраняется</b>\n"
        f"{html.escape(scenario_title)} · {html.escape(channel_name)} · "
        f"<code>channel_id={channel_id}</code>\n"
        f"Источник: {html.escape(source)}</blockquote>"
    )


def subscriber_preview_home_text(
    *,
    channel_name: str,
    channel_id: int,
    active_revision_id: int | None,
    draft_count: int,
) -> str:
    context = customization_context_header(
        channel_name=channel_name,
        channel_id=channel_id,
        active_revision_id=active_revision_id,
        draft_count=draft_count,
    )
    source = (
        "Ниже будет показан черновик поверх опубликованной версии."
        if draft_count > 0
        else "Черновика нет, поэтому показывается опубликованная версия."
    )
    return (
        "<b>Посмотреть глазами подписчика</b>\n"
        f"{context}\n\n"
        f"{source}\n\n"
        "Это безопасный режим: он не создаёт подписчика или topic, не меняет "
        "активную предложку пользователя и не записывает сообщения/аналитику. "
        "Кнопки внутри примеров не меняют реальные настройки."
    )


def subscriber_preview_section_title(title: str) -> str:
    return f"<b>{html.escape(title)}</b>"


async def render_subscriber_preview_scenario(
    *, db, channel_id: int, scenario_key: str, notice_text: str | None = None
) -> str:
    """Render one draft-aware sample without mutating application state.

    Cleanup notices are not part of Channel Custom Pack: production scheduler
    sends ``channels.notice_text`` verbatim. The preview therefore receives that
    value explicitly instead of pretending that ``cleanup.notice`` is live.
    """
    scenario = SUBSCRIBER_PREVIEW_BY_KEY.get(scenario_key)
    if scenario is None:
        raise KeyError(scenario_key)
    if scenario_key == "admin_reply":
        return html.escape(SAMPLE_ADMIN_REPLY)
    if scenario_key == "cleanup":
        value = (notice_text or "").strip()
        return (
            html.escape(value)
            if value
            else "<i>Предупреждение не задано — подписчик ничего не получит.</i>"
        )
    if scenario.template_key is None:
        raise ValueError(f"Scenario {scenario_key!r} is not text-renderable")
    return await render_template(
        db,
        channel_id,
        scenario.template_key,
        include_draft=True,
        **scenario.values(),
    )
