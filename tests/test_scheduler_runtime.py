import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scheduler import ChannelScheduler


class SchedulerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_cleanup_does_not_warn_or_clean(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        db = SimpleNamespace()
        cleaner = SimpleNamespace(cleanup_by_policy=AsyncMock())
        scheduler = ChannelScheduler(
            bot=bot, db=db, cleaner=cleaner, check_seconds=60
        )
        channel = {
            "channel_id": 5,
            "auto_cleanup_enabled": 0,
            "next_reset_at": "2026-08-01T00:00:00+00:00",
        }

        await scheduler._process_channel(channel, datetime.now(timezone.utc))

        bot.send_message.assert_not_awaited()
        cleaner.cleanup_by_policy.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
