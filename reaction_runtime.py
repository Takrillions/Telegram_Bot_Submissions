"""Administrator reaction routing for subscriber forum messages."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ReplyParameters

from authorization import ChannelAction
from templates import render_template

logger = logging.getLogger(__name__)


def reaction_key_and_label(reaction) -> tuple[str, str]:
    kind = str(getattr(reaction, "type", ""))
    if kind == "emoji":
        emoji = str(getattr(reaction, "emoji", ""))
        return f"emoji:{emoji}", emoji or "реакция"
    if kind == "custom_emoji":
        custom_id = str(getattr(reaction, "custom_emoji_id", ""))
        return f"custom:{custom_id}", "кастомная реакция"
    if kind == "paid":
        return "paid", "⭐"
    return f"other:{kind}", "реакция"


def added_reactions(update) -> list[tuple[str, str]]:
    old_keys = {reaction_key_and_label(item)[0] for item in update.old_reaction}
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in update.new_reaction:
        key, label = reaction_key_and_label(item)
        if key not in old_keys and key not in seen:
            seen.add(key)
            result.append((key, label))
    return result


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _missing_topic_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "message thread not found", "message thread is not found", "forum topic not found",
        "topic_closed", "topic closed", "message thread is closed",
    ))


def _message_id(result) -> int:
    return int(getattr(result, "message_id"))


class ReactionRuntime:
    def __init__(self, *, bot, db, authorizer) -> None:
        self.bot = bot
        self.db = db
        self.authorizer = authorizer

    async def handle(self, update) -> str:
        """Handle one public reaction update; returns a compact status for tests/logs."""
        actor = getattr(update, "user", None)
        if actor is None or bool(getattr(actor, "is_bot", False)):
            return "ignored_actor"
        group_id = int(update.chat.id)
        source = await self.db.get_reaction_source(
            group_id=group_id, forum_message_id=int(update.message_id)
        )
        if source is None:
            # General, admin messages and service topics have no subscriber-source mapping.
            return "ignored_source"
        channel_id = int(source["channel_id"])
        decision = await self.authorizer.require(
            actor_id=int(actor.id), channel_id=channel_id,
            action=ChannelAction.REACTION_TRIGGER, context_group_id=group_id,
            require_current_telegram_admin=True,
        )
        if not decision.allowed:
            return "ignored_permissions"

        added = added_reactions(update)
        if not added:
            return "removed_or_unchanged"
        settings = await self.db.get_channel_reaction_settings(channel_id)
        mode = str(settings["mode"])
        event_at = _aware(update.date)

        accepted: list[tuple[str, str]] = []
        for key, label in added:
            if await self.db.record_reaction_event(
                channel_id=channel_id, group_id=group_id,
                source_message_id=int(update.message_id), actor_id=int(actor.id),
                reaction_key=key, event_at=event_at, mode=mode,
            ):
                accepted.append((key, label))
        if not accepted:
            return "duplicate"

        if mode == "subscriber":
            for _, label in accepted:
                try:
                    await self.bot.send_message(
                        chat_id=int(source["user_id"]),
                        text=await render_template(
                            self.db, channel_id, "reaction.subscriber_notification", reaction=label
                        ),
                    )
                except TelegramAPIError:
                    logger.warning(
                        "Unable to deliver reaction notification channel=%s message=%s",
                        channel_id, update.message_id,
                    )
            return "subscriber_notified"

        service_topic_id = settings.get("service_topic_id")
        if service_topic_id is None or bool(settings.get("requires_repair")):
            return "repair_required"
        reaction_key = accepted[0][0]
        claimed = await self.db.claim_reaction_dispatch(
            channel_id=channel_id, group_id=group_id,
            source_message_id=int(update.message_id), service_topic_id=int(service_topic_id),
            triggered_by=int(actor.id), reaction_key=reaction_key,
        )
        if not claimed:
            return "already_dispatched"

        destination_message_id: int | None = None
        try:
            if str(source["privacy_mode"]) == "anonymous":
                copied = await self.bot.copy_message(
                    chat_id=group_id, message_thread_id=int(service_topic_id),
                    from_chat_id=group_id, message_id=int(update.message_id),
                )
                destination_message_id = _message_id(copied)
            else:
                try:
                    forwarded = await self.bot.forward_message(
                        chat_id=group_id, message_thread_id=int(service_topic_id),
                        from_chat_id=group_id, message_id=int(update.message_id),
                    )
                    destination_message_id = _message_id(forwarded)
                except TelegramBadRequest:
                    copied = await self.bot.copy_message(
                        chat_id=group_id, message_thread_id=int(service_topic_id),
                        from_chat_id=group_id, message_id=int(update.message_id),
                    )
                    destination_message_id = _message_id(copied)
        except TelegramAPIError as exc:
            await self.db.fail_reaction_dispatch(
                channel_id=channel_id, group_id=group_id,
                source_message_id=int(update.message_id), error_code=exc.__class__.__name__,
            )
            if _missing_topic_error(exc):
                await self.db.mark_reaction_service_topic_unavailable(channel_id=channel_id)
            logger.warning(
                "Reaction service delivery failed channel=%s message=%s: %s",
                channel_id, update.message_id, exc,
            )
            return "delivery_failed"

        if destination_message_id is None:
            await self.db.fail_reaction_dispatch(
                channel_id=channel_id, group_id=group_id,
                source_message_id=int(update.message_id), error_code="missing_destination_id",
            )
            return "delivery_failed"
        await self.db.complete_reaction_dispatch(
            channel_id=channel_id, group_id=group_id,
            source_message_id=int(update.message_id), destination_message_id=destination_message_id,
        )

        if str(source["privacy_mode"]) == "anonymous":
            tag = await self.db.get_anonymous_tag(
                channel_id=channel_id, user_id=int(source["user_id"])
            )
            if tag:
                try:
                    await self.bot.send_message(
                        chat_id=group_id, message_thread_id=int(service_topic_id),
                        text=await render_template(
                            self.db, channel_id, "reaction.service_anonymous_source", anonymous_tag=tag
                        ),
                        reply_parameters=ReplyParameters(message_id=destination_message_id),
                    )
                except TelegramAPIError:
                    logger.warning("Unable to append anonymous tag to reaction service topic")
        return "service_dispatched"
