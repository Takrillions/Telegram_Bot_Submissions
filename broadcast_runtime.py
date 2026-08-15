"""Idempotent channel-scoped mass broadcast delivery."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BroadcastSummary:
    broadcast_id: str
    unique_recipients: int
    delivered: int
    undelivered: int
    skipped: int
    errors: int


class BroadcastRuntime:
    """Deliver one stored Telegram message to current subscriber topics.

    Recipient identities are snapshotted when the draft is claimed for send.
    Before every Telegram call the delivery row moves from ``pending`` to
    ``reserved`` and stores the subscriber's *current* privacy/topic route.
    This deliberately prefers at-most-once delivery after a crash: a reserved
    row is never re-sent on resume, avoiding duplicate publications.
    """

    def __init__(self, *, bot, db) -> None:
        self.bot = bot
        self.db = db
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def deliver(self, *, broadcast_id: str, channel_id: int) -> BroadcastSummary:
        async with self._channel_locks[channel_id]:
            broadcast = await self.db.get_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
            if broadcast is None:
                raise ValueError("Unknown broadcast")
            if str(broadcast["status"]) == "completed":
                return await self._summary(broadcast_id=broadcast_id, channel_id=channel_id)
            if str(broadcast["status"]) != "sending":
                raise ValueError("Broadcast is not sending")

            source_chat_id = int(broadcast["source_chat_id"])
            source_message_ids = self.db.broadcast_source_message_ids(broadcast)
            pending = await self.db.list_pending_broadcast_deliveries(
                broadcast_id=broadcast_id, channel_id=channel_id
            )

            for delivery in pending:
                user_id = int(delivery["user_id"])
                target = await self.db.reserve_broadcast_delivery(
                    broadcast_id=broadcast_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                if target is None:
                    continue

                topic_id = target["topic_id"]
                group_id = target["group_id"]
                topic_status = target["topic_status"]
                if topic_id is None or group_id is None or str(topic_status or "") == "closed":
                    await self.db.complete_broadcast_delivery(
                        broadcast_id=broadcast_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        status="undelivered",
                        error_code="current_topic_unavailable",
                    )
                    continue

                try:
                    if len(source_message_ids) == 1:
                        await self.bot.copy_message(
                            chat_id=int(group_id),
                            message_thread_id=int(topic_id),
                            from_chat_id=source_chat_id,
                            message_id=source_message_ids[0],
                        )
                    else:
                        copied = await self.bot.copy_messages(
                            chat_id=int(group_id),
                            message_thread_id=int(topic_id),
                            from_chat_id=source_chat_id,
                            message_ids=list(source_message_ids),
                        )
                        if copied is not None and len(copied) != len(source_message_ids):
                            raise RuntimeError("partial_album_copy")
                except Exception as exc:  # one Telegram failure must not stop the batch
                    detail = str(exc).lower()
                    unavailable_topic = any(marker in detail for marker in (
                        "message thread not found", "message thread is not found",
                        "forum topic not found", "topic_closed", "topic closed",
                        "message thread is closed",
                    ))
                    await self.db.complete_broadcast_delivery(
                        broadcast_id=broadcast_id,
                        channel_id=channel_id,
                        user_id=user_id,
                        status="undelivered" if unavailable_topic else "error",
                        error_code="current_topic_unavailable" if unavailable_topic else type(exc).__name__,
                    )
                    continue

                await self.db.complete_broadcast_delivery(
                    broadcast_id=broadcast_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    status="delivered",
                )

            await self.db.finish_broadcast(broadcast_id=broadcast_id, channel_id=channel_id)
            return await self._summary(broadcast_id=broadcast_id, channel_id=channel_id)

    async def _summary(self, *, broadcast_id: str, channel_id: int) -> BroadcastSummary:
        counts = await self.db.get_broadcast_delivery_summary(
            broadcast_id=broadcast_id,
            channel_id=channel_id,
        )
        return BroadcastSummary(
            broadcast_id=broadcast_id,
            unique_recipients=int(counts["unique_recipients"]),
            delivered=int(counts["delivered"]),
            undelivered=int(counts["undelivered"]),
            skipped=int(counts["skipped"]),
            errors=int(counts["errors"]),
        )
