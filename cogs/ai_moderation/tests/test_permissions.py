"""
Unit tests for the permission system.

Tests authorization, hierarchy, protected users, self-targeting, and
owner protection.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..config import GuildConfig
from ..exceptions import (
    BotMissingPermissionError,
    HierarchyError,
    OwnerTargetError,
    ProtectedRoleError,
    ProtectedUserError,
    SelfTargetError,
    UnauthorizedModerator,
)
from ..models import ActionType
from ..permissions import PermissionChecker


def _make_member(
    member_id: int = 100,
    *,
    guild_owner_id: int = 1,
    roles: list = None,
    is_admin: bool = False,
    perms: dict = None,
) -> MagicMock:
    """Create a mock discord.Member."""
    member = MagicMock()
    member.id = member_id
    member.display_name = f"User{member_id}"
    member.mention = f"<@{member_id}>"
    member.guild = MagicMock()
    member.guild.owner_id = guild_owner_id
    member.roles = roles or []

    # Set up guild_permissions.
    guild_perms = MagicMock()
    guild_perms.administrator = is_admin
    default_perms = {
        "moderate_members": False,
        "kick_members": False,
        "ban_members": False,
        "manage_messages": False,
        "manage_roles": False,
        "manage_channels": False,
        "manage_guild": False,
    }
    if perms:
        default_perms.update(perms)
    for perm_name, perm_value in default_perms.items():
        setattr(guild_perms, perm_name, perm_value)
    member.guild_permissions = guild_perms

    # Top role for hierarchy.
    top_role = MagicMock()
    top_role.position = roles[0].position if roles else 0
    member.top_role = top_role

    return member


def _make_role(role_id: int, position: int = 1, name: str = "Role") -> MagicMock:
    """Create a mock discord.Role."""
    role = MagicMock()
    role.id = role_id
    role.position = position
    role.name = name
    return role


class TestAuthorization:
    def test_administrator_always_authorized(self):
        checker = PermissionChecker(bot_user_id=999)
        member = _make_member(is_admin=True)
        config = GuildConfig()
        assert checker.is_authorized_moderator(member, config)

    def test_authorized_role(self):
        checker = PermissionChecker(bot_user_id=999)
        role = _make_role(role_id=500, position=5)
        member = _make_member(member_id=100, roles=[role])
        config = GuildConfig(authorized_role_ids={500})
        assert checker.is_authorized_moderator(member, config)

    def test_mod_permission_authorized(self):
        checker = PermissionChecker(bot_user_id=999)
        member = _make_member(perms={"moderate_members": True})
        config = GuildConfig()
        assert checker.is_authorized_moderator(member, config)

    def test_unauthorized_user(self):
        checker = PermissionChecker(bot_user_id=999)
        member = _make_member()  # no perms, no roles
        config = GuildConfig()
        assert not checker.is_authorized_moderator(member, config)


class TestHierarchy:
    def test_owner_can_moderate_anyone(self):
        checker = PermissionChecker(bot_user_id=999)
        owner = _make_member(member_id=1, guild_owner_id=1, is_admin=True)
        target = _make_member(member_id=2, guild_owner_id=1)
        assert checker._can_moderate(owner, target)

    def test_higher_role_can_moderate_lower(self):
        checker = PermissionChecker(bot_user_id=999)
        high_role = _make_role(1, position=10)
        low_role = _make_role(2, position=1)
        actor = _make_member(member_id=100, roles=[high_role])
        target = _make_member(member_id=200, roles=[low_role])
        assert checker._can_moderate(actor, target)

    def test_lower_role_cannot_moderate_higher(self):
        checker = PermissionChecker(bot_user_id=999)
        high_role = _make_role(1, position=10)
        low_role = _make_role(2, position=1)
        actor = _make_member(member_id=100, roles=[low_role])
        target = _make_member(member_id=200, roles=[high_role])
        assert not checker._can_moderate(actor, target)

    def test_equal_role_cannot_moderate(self):
        checker = PermissionChecker(bot_user_id=999)
        role = _make_role(1, position=5)
        actor = _make_member(member_id=100, roles=[role])
        target = _make_member(member_id=200, roles=[role])
        assert not checker._can_moderate(actor, target)


class TestValidateAction:
    def test_unauthorized_user_rejected(self):
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member()  # no perms
        target = _make_member(member_id=200)
        guild = MagicMock()
        config = GuildConfig()
        with pytest.raises(UnauthorizedModerator):
            checker.validate_action(ActionType.WARN, actor, target, guild, config)

    def test_self_target_rejected(self):
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member(member_id=100, is_admin=True)
        guild = MagicMock()
        config = GuildConfig()
        with pytest.raises(SelfTargetError):
            checker.validate_action(ActionType.BAN, actor, actor, guild, config)

    def test_owner_target_rejected(self):
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member(member_id=100, is_admin=True, guild_owner_id=1)
        target = _make_member(member_id=1, guild_owner_id=1)
        guild = MagicMock()
        guild.owner_id = 1
        config = GuildConfig()
        with pytest.raises(OwnerTargetError):
            checker.validate_action(ActionType.BAN, actor, target, guild, config)

    def test_protected_user_rejected(self):
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member(member_id=100, is_admin=True, guild_owner_id=1)
        target = _make_member(member_id=200, guild_owner_id=1)
        guild = MagicMock()
        config = GuildConfig(protected_user_ids={200})
        with pytest.raises(ProtectedUserError):
            checker.validate_action(ActionType.WARN, actor, target, guild, config)

    def test_protected_role_rejected(self):
        checker = PermissionChecker(bot_user_id=999)
        protected_role = _make_role(500, position=1, name="Protected")
        actor = _make_member(member_id=100, is_admin=True, guild_owner_id=1)
        target = _make_member(member_id=200, guild_owner_id=1, roles=[protected_role])
        guild = MagicMock()
        config = GuildConfig(protected_role_ids={500})
        with pytest.raises(ProtectedRoleError):
            checker.validate_action(ActionType.WARN, actor, target, guild, config)

    def test_hierarchy_failure(self):
        checker = PermissionChecker(bot_user_id=999)
        high_role = _make_role(1, position=10)
        low_role = _make_role(2, position=1)
        actor = _make_member(
            member_id=100, roles=[low_role],
            perms={"moderate_members": True}, guild_owner_id=1,
        )
        target = _make_member(member_id=200, roles=[high_role], guild_owner_id=1)
        guild = MagicMock()
        guild.owner_id = 1
        # Mock the bot member as having admin.
        bot_member = _make_member(member_id=999, is_admin=True, guild_owner_id=1)
        guild.me = bot_member
        config = GuildConfig()
        with pytest.raises(HierarchyError):
            checker.validate_action(ActionType.TIMEOUT, actor, target, guild, config)

    def test_self_safe_action_allowed(self):
        """Untimeout on self should be allowed."""
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member(member_id=100, is_admin=True, guild_owner_id=1)
        guild = MagicMock()
        config = GuildConfig()
        # Should not raise.
        checker.validate_action(ActionType.UNTIMEOUT, actor, actor, guild, config)

    def test_bot_missing_permission(self):
        checker = PermissionChecker(bot_user_id=999)
        actor = _make_member(member_id=100, is_admin=True, guild_owner_id=1)
        target = _make_member(member_id=200, guild_owner_id=1)
        guild = MagicMock()
        guild.owner_id = 1
        # Bot member lacks moderate_members.
        bot_perms = MagicMock()
        bot_perms.administrator = False
        bot_perms.moderate_members = False
        bot_perms.ban_members = False
        bot_member = MagicMock()
        bot_member.guild_permissions = bot_perms
        guild.me = bot_member
        config = GuildConfig()
        with pytest.raises(BotMissingPermissionError):
            checker.validate_action(ActionType.WARN, actor, target, guild, config)
