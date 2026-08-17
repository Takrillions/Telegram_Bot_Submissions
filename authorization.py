"""Channel-scoped authorization for the administrative bot surface.

``channels.owner_id`` is deliberately the only persisted source of truth for
the main administrator.  Membership is *not* persisted: ordinary
administrators are authorised against the current Telegram membership for
every sensitive action, so a stale inline button cannot retain access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol


class ChannelLike(Protocol):
    def __getitem__(self, key: str) -> object: ...


class GlobalRole(str, Enum):
    SUPERADMIN = "superadmin"


class GlobalAction(str, Enum):
    SUPERADMIN_PANEL = "superadmin_panel"
    PRESTART_PROFILE = "prestart_profile"
    STANDARD_PACK = "standard_pack"


@dataclass(frozen=True)
class GlobalAuthorizationResult:
    role: GlobalRole | None
    action: GlobalAction
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.role is GlobalRole.SUPERADMIN


def parse_superadmin_telegram_id(raw_value: str | None) -> int | None:
    """Parse SUPERADMIN_TELEGRAM_ID without ever granting access on bad input."""
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def is_superadmin(actor_id: int | None, configured_superadmin_id: int | None) -> bool:
    """Return True only for the one explicitly configured global bot owner.

    The global role is deliberately independent from channel ownership and
    Telegram group-administrator status.  Missing/invalid configuration is
    represented as ``None`` and therefore fails closed.
    """
    return (
        isinstance(actor_id, int)
        and isinstance(configured_superadmin_id, int)
        and configured_superadmin_id > 0
        and actor_id == configured_superadmin_id
    )


class GlobalAuthorizer:
    """Authorization gate for settings that belong to the bot account itself."""

    def __init__(self, *, superadmin_telegram_id: int | None) -> None:
        self.superadmin_telegram_id = (
            superadmin_telegram_id
            if isinstance(superadmin_telegram_id, int) and superadmin_telegram_id > 0
            else None
        )

    def is_superadmin(self, actor_id: int | None) -> bool:
        return is_superadmin(actor_id, self.superadmin_telegram_id)

    def require(
        self,
        *,
        actor_id: int | None,
        action: GlobalAction,
    ) -> GlobalAuthorizationResult:
        if self.superadmin_telegram_id is None:
            return GlobalAuthorizationResult(None, action, "superadmin_not_configured")
        if not self.is_superadmin(actor_id):
            return GlobalAuthorizationResult(None, action, "superadmin_required")
        return GlobalAuthorizationResult(GlobalRole.SUPERADMIN, action)


class ChannelRole(str, Enum):
    SUBSCRIBER = "subscriber"
    ADMIN = "admin"
    OWNER = "owner"


class ChannelAction(str, Enum):
    SUBSCRIBER = "subscriber"
    MODERATION = "moderation"
    ADMIN_REPLY = "admin_reply"
    PANEL = "panel"
    SETTINGS = "settings"
    STATISTICS = "statistics"
    EXPORT = "export"
    SEARCH = "search"
    BROADCAST = "broadcast"
    REACTION_SETTINGS = "reaction_settings"
    REACTION_TRIGGER = "reaction_trigger"


# This is intentionally conservative.  Settings, analytics/export and future
# channel-wide features remain owner-only; operating an individual subscriber
# thread is available to a current group administrator.
OWNER_ONLY_ACTIONS = frozenset({
    ChannelAction.PANEL,
    ChannelAction.SETTINGS,
    ChannelAction.STATISTICS,
    ChannelAction.EXPORT,
    ChannelAction.SEARCH,
    ChannelAction.BROADCAST,
    ChannelAction.REACTION_SETTINGS,
})
ADMIN_ACTIONS = frozenset({
    ChannelAction.SUBSCRIBER,
    ChannelAction.MODERATION,
    ChannelAction.ADMIN_REPLY,
    ChannelAction.REACTION_TRIGGER,
})


@dataclass(frozen=True)
class AuthorizationResult:
    channel: ChannelLike | None
    role: ChannelRole | None
    current_telegram_admin: bool
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.channel is not None and self.role is not None


MemberResolver = Callable[[int, int], Awaitable[bool]]


class ChannelAuthorizer:
    """One server-side policy gate for every channel-scoped action.

    ``member_resolver`` is injectable for unit tests.  The production adapter
    calls ``bot.get_chat_member`` and never caches decisions, because dynamic
    Telegram role changes must invalidate old callbacks immediately.
    """

    def __init__(self, *, db, member_resolver: MemberResolver) -> None:
        self.db = db
        self.member_resolver = member_resolver

    async def resolve_role(
        self,
        *,
        actor_id: int | None,
        channel_id: int,
        context_group_id: int | None = None,
        require_current_telegram_admin: bool = False,
    ) -> AuthorizationResult:
        if actor_id is None:
            return AuthorizationResult(None, None, False, "missing_actor")
        channel = await self.db.get_channel_by_id(channel_id)
        if channel is None or not bool(channel["enabled"]):
            return AuthorizationResult(None, None, False, "channel_unavailable")
        group_id = int(channel["group_id"])
        if context_group_id is not None and group_id != context_group_id:
            return AuthorizationResult(None, None, False, "wrong_channel_context")

        is_owner = int(channel["owner_id"]) == actor_id
        # A plain role lookup may use the persisted owner record, but every
        # owner-only action passes require_current_telegram_admin=True through
        # require(), including actions initiated from the private panel.
        must_check_membership = context_group_id is not None or not is_owner or require_current_telegram_admin
        current_admin = False
        if must_check_membership:
            try:
                current_admin = bool(await self.member_resolver(group_id, actor_id))
            except Exception:
                return AuthorizationResult(channel, None, False, "membership_unavailable")
        if is_owner:
            if require_current_telegram_admin and not current_admin:
                return AuthorizationResult(channel, None, False, "owner_no_longer_group_admin")
            return AuthorizationResult(channel, ChannelRole.OWNER, current_admin)
        if current_admin:
            return AuthorizationResult(channel, ChannelRole.ADMIN, True)
        return AuthorizationResult(channel, None, False, "not_current_group_admin")

    async def require(
        self,
        *,
        actor_id: int | None,
        channel_id: int,
        action: ChannelAction,
        context_group_id: int | None = None,
        require_current_telegram_admin: bool = False,
    ) -> AuthorizationResult:
        # System/owner actions require a live Telegram-admin check even from
        # the private panel.  The stored owner_id is never transferred, but it
        # alone is not sufficient to execute a system command after removal.
        require_live_membership = require_current_telegram_admin or action in OWNER_ONLY_ACTIONS
        result = await self.resolve_role(
            actor_id=actor_id,
            channel_id=channel_id,
            context_group_id=context_group_id,
            require_current_telegram_admin=require_live_membership,
        )
        if not result.allowed:
            return result
        if action in OWNER_ONLY_ACTIONS and result.role != ChannelRole.OWNER:
            return AuthorizationResult(result.channel, None, result.current_telegram_admin, "owner_required")
        if action not in OWNER_ONLY_ACTIONS | ADMIN_ACTIONS:
            return AuthorizationResult(result.channel, None, result.current_telegram_admin, "unknown_action")
        return result


def permission_matrix() -> dict[ChannelRole, frozenset[ChannelAction]]:
    """Machine-readable matrix used by tests and future command scopes."""
    return {
        ChannelRole.SUBSCRIBER: frozenset(),
        ChannelRole.ADMIN: ADMIN_ACTIONS,
        ChannelRole.OWNER: OWNER_ONLY_ACTIONS | ADMIN_ACTIONS,
    }
