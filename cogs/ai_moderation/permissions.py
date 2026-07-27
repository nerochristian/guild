"""
Permission and hierarchy validation.

All permission checks go through this module before any action executes.
This is the single enforcement point for:
- Discord guild permissions
- Bot-specific authorization roles
- Role hierarchy (actor vs target, bot vs target)
- Protected users and roles
- Self-targeting rules
- Server owner protection
- Maximum punishment limits per moderator role
"""
from __future__ import annotations

import logging
from typing import Optional, Set, Union

import discord

from .config import GuildConfig
from .exceptions import (
    BotMissingPermissionError,
    HierarchyError,
    OwnerTargetError,
    ProtectedRoleError,
    ProtectedUserError,
    SelfTargetError,
    UnauthorizedModerator,
)
from .models import ActionType
from .utils import get_reverse_action

logger = logging.getLogger("ModBot.AIModeration.Permissions")


# =============================================================================
# Discord permission requirements per action
# =============================================================================


_ACTION_PERMISSIONS: dict[ActionType, str] = {
    ActionType.WARN: "moderate_members",
    ActionType.TIMEOUT: "moderate_members",
    ActionType.UNTIMEOUT: "moderate_members",
    ActionType.KICK: "kick_members",
    ActionType.BAN: "ban_members",
    ActionType.TEMP_BAN: "ban_members",
    ActionType.UNBAN: "ban_members",
    ActionType.PURGE: "manage_messages",
    ActionType.DELETE_MESSAGE: "manage_messages",
    ActionType.ADD_ROLE: "manage_roles",
    ActionType.REMOVE_ROLE: "manage_roles",
    ActionType.ADD_MUTED_ROLE: "manage_roles",
    ActionType.REMOVE_MUTED_ROLE: "manage_roles",
    ActionType.SLOWMODE: "manage_channels",
    ActionType.LOCK_CHANNEL: "manage_channels",
    ActionType.UNLOCK_CHANNEL: "manage_channels",
    ActionType.ESCALATE_HUMAN: "moderate_members",  # just needs to be a mod
    ActionType.UNDO: "moderate_members",
}

# Actions that are safe to self-target.
_SELF_SAFE_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.UNTIMEOUT,
    ActionType.REMOVE_MUTED_ROLE,
    ActionType.REMOVE_ROLE,
})

# Actions that don't need a target member (channel-scoped or informational).
_NO_TARGET_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.SLOWMODE,
    ActionType.LOCK_CHANNEL,
    ActionType.UNLOCK_CHANNEL,
    ActionType.PURGE,
    ActionType.ESCALATE_HUMAN,
    ActionType.NO_ACTION,
})


class PermissionChecker:
    """Centralized permission and hierarchy validator."""

    def __init__(self, bot_user_id: int) -> None:
        self.bot_user_id = bot_user_id

    # ------------------------------------------------------------------
    # Authorization — is this user allowed to use AI mod at all?
    # ------------------------------------------------------------------

    def is_authorized_moderator(
        self,
        member: discord.Member,
        config: GuildConfig,
    ) -> bool:
        """Check if the member is authorized to use AI moderation.

        Authorization is granted if ANY of:
        - The member is the bot owner (checked elsewhere via is_bot_owner_id).
        - The member has administrator permission.
        - The member has any of the configured authorized roles.
        - The member has any moderation-related Discord permission.
        """
        # Administrator always passes.
        if member.guild_permissions.administrator:
            return True

        # Check configured authorized roles.
        if config.authorized_role_ids:
            member_role_ids = {r.id for r in member.roles}
            if member_role_ids & config.authorized_role_ids:
                return True

        # Check standard moderation permissions.
        mod_perms = (
            "moderate_members", "kick_members", "ban_members",
            "manage_messages", "manage_roles", "manage_channels",
            "manage_guild",
        )
        if any(getattr(member.guild_permissions, p, False) for p in mod_perms):
            return True

        return False

    def is_senior_moderator(
        self,
        member: discord.Member,
        config: GuildConfig,
    ) -> bool:
        """Check if the member is a senior moderator (can confirm severe actions)."""
        if member.guild_permissions.administrator:
            return True
        if config.senior_moderator_role_ids:
            member_role_ids = {r.id for r in member.roles}
            if member_role_ids & config.senior_moderator_role_ids:
                return True
        return False

    # ------------------------------------------------------------------
    # Full validation — called before every action
    # ------------------------------------------------------------------

    def validate_action(
        self,
        action: ActionType,
        actor: discord.Member,
        target: Optional[discord.Member],
        guild: discord.Guild,
        config: GuildConfig,
        *,
        is_undo: bool = False,
    ) -> None:
        """Validate that an action can be executed. Raises on failure.

        This is the master check — it runs ALL of:
        1. Moderator authorization
        2. Discord permission for the specific action
        3. Bot has the required permission
        4. Self-target safety
        5. Server owner protection
        6. Protected user/role checks
        7. Role hierarchy (actor vs target, bot vs target)
        """
        # 1. Authorization.
        if not self.is_authorized_moderator(actor, config):
            raise UnauthorizedModerator(actor.id)

        # 2. Actor has the Discord permission.
        required_perm = _ACTION_PERMISSIONS.get(action)
        if required_perm and not actor.guild_permissions.administrator:
            if not getattr(actor.guild_permissions, required_perm, False):
                raise BotMissingPermissionError(
                    required_perm.replace("_", " ").title()
                )

        # 3. Bot has the Discord permission.
        bot_member = guild.me
        if bot_member and required_perm:
            if not bot_member.guild_permissions.administrator:
                if not getattr(bot_member.guild_permissions, required_perm, False):
                    raise BotMissingPermissionError(
                        required_perm.replace("_", " ").title()
                    )

        # 4-7. Target-specific checks (skip for no-target actions).
        if action in _NO_TARGET_ACTIONS or action == ActionType.NO_ACTION:
            return

        if target is None:
            # Some actions can proceed without a resolved target (e.g. unban
            # by ID when the user left). The executor handles that case.
            return

        # 4. Self-target.
        if actor.id == target.id and action not in _SELF_SAFE_ACTIONS:
            raise SelfTargetError()

        # 5. Server owner protection.
        if target.id == guild.owner_id and actor.id != guild.owner_id:
            raise OwnerTargetError()
        if target.id == guild.owner_id and not is_undo:
            # Even the owner shouldn't be punished by the bot.
            raise OwnerTargetError()

        # 6. Protected users and roles.
        if target.id in config.protected_user_ids:
            raise ProtectedUserError(target.display_name)

        target_role_ids = {r.id for r in target.roles}
        for role_id in target_role_ids & config.protected_role_ids:
            role = guild.get_role(role_id)
            role_name = role.name if role else f"ID:{role_id}"
            raise ProtectedRoleError(role_name)

        # 7. Role hierarchy.
        # Actor must be above target (unless actor is owner or admin with dot override).
        if not self._can_moderate(actor, target):
            raise HierarchyError(actor.display_name, target.display_name)

        # Bot must be above target.
        if bot_member and not self._can_moderate(bot_member, target):
            raise HierarchyError(
                bot_member.display_name, target.display_name
            )

    # ------------------------------------------------------------------
    # Hierarchy helper
    # ------------------------------------------------------------------

    @staticmethod
    def _can_moderate(actor: discord.Member, target: discord.Member) -> bool:
        """Check if actor can moderate target per Discord's hierarchy rules.

        The actor can moderate the target if:
        - Actor is the guild owner.
        - Actor has administrator permission.
        - Actor's top role is strictly higher than target's top role.
        - Actor is the target (for self-safe actions, checked by caller).
        """
        if actor.id == target.guild.owner_id:
            return True
        if actor.guild_permissions.administrator:
            return True
        if actor.top_role <= target.top_role:
            return False
        return True

    # ------------------------------------------------------------------
    # Can the moderator execute this specific action?
    # ------------------------------------------------------------------

    def can_execute(
        self,
        action: ActionType,
        actor: discord.Member,
        target: Optional[discord.Member],
        guild: discord.Guild,
        config: GuildConfig,
    ) -> bool:
        """Non-raising version of validate_action. Returns True/False."""
        try:
            self.validate_action(action, actor, target, guild, config)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Check if the bot itself is protected from moderation
    # ------------------------------------------------------------------

    def is_bot(self, user_id: int) -> bool:
        """Check if a user ID is the bot itself."""
        return user_id == self.bot_user_id

    def validate_not_bot(self, target_id: int) -> None:
        """Ensure the target is not the bot itself."""
        if target_id == self.bot_user_id:
            raise SelfTargetError()
