import discord
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional, Union, Tuple
import logging
import json

from utils.embeds import ModEmbed, Colors, compact_kv_lines
from utils.checks import is_bot_owner_id, get_owner_ids
from utils.logging import send_log_embed
from utils.status_emojis import apply_status_emoji_overrides
from utils.components_v2 import ensure_layout_view_action_rows, layout_view_from_embeds

logger = logging.getLogger(__name__)

class HelperCommands:
    @staticmethod
    def _classify_log_type_from_title(embed: discord.Embed, default: str = "mod") -> str:
        title = (getattr(embed, "title", "") or "").strip().lower()
        if not title:
            return default

        if (
            title == "message deleted"
            or title.endswith("messages deleted")
            or title == "bulk message delete"
            or title == "message edited"
        ):
            return "message"

        # Moderation action cards belong in mod logs.
        mod_markers = (
            "user kicked",
            "user banned",
            "user softbanned",
            "user temporarily banned",
            "user unbanned",
            "member kicked",
            "member banned",
            "member unbanned",
            "user timed out",
            "user timeout removed",
            "user warned",
            "user muted",
            "user unmuted",
            "user quarantined",
            "quarantine lifted",
            "mass ban",
            "moderator purge",
        )
        if any(marker in title for marker in mod_markers):
            return "mod"

        audit_markers = (
            "permissions updated",
            "channel created",
            "channel deleted",
            "role created",
            "role deleted",
            "role updated",
            "roles updated",
            "role name update",
            "webhook created",
            "webhook deleted",
            "webhook updated",
            "emoji created",
            "emoji deleted",
            "emoji updated",
            "emoji removed",
            "sticker created",
            "sticker deleted",
            "sticker updated",
            "invite created",
            "invite deleted",
            "invite updated",
            "nickname changed",
            "member joined",
            "member left",
        )
        if any(marker in title for marker in audit_markers):
            return "audit"

        return default

    async def _respond(
        self,
        source: Union[discord.Interaction, commands.Context],
        *,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        ephemeral: bool = False,
        **kwargs,
    ):
        """Send a response or followup depending on whether the interaction/context is already acknowledged."""
        if embed is not None:
            try:
                embed = await apply_status_emoji_overrides(embed, getattr(source, "guild", None))
            except Exception:
                pass

            existing_view = kwargs.pop("view", None)
            layout = await layout_view_from_embeds(
                content=content,
                embed=embed,
                existing_view=existing_view,
            )
            kwargs["view"] = ensure_layout_view_action_rows(layout)
            content = None
            embed = None

        try:
            if isinstance(source, discord.Interaction):
                if source.response.is_done():
                    return await source.followup.send(
                        content=content, embed=embed, ephemeral=ephemeral, **kwargs
                    )
                return await source.response.send_message(
                    content=content, embed=embed, ephemeral=ephemeral, **kwargs
                )
            else:
                # Context
                return await source.reply(
                    content=content,
                    embed=embed,
                    mention_author=False,
                    **kwargs,
                )
        except discord.HTTPException:
            # Fallback when the interaction state changed mid-execution.
            if isinstance(source, discord.Interaction):
                try:
                    return await source.followup.send(
                        content=content, embed=embed, ephemeral=ephemeral, **kwargs
                    )
                except Exception:
                    pass

    async def log_action(self, guild: discord.Guild, embed: discord.Embed, log_type: str = "mod", view: Optional[discord.ui.View] = None) -> None:
        """
        Log moderation action to configured channel
        log_type: "mod", "voice", or "audit"
        """
        try:
            # Caller defaults to mod; promote obviously audit/message titles so they
            # do not pollute mod logs.
            resolved_log_type = log_type
            if log_type == "mod":
                resolved_log_type = self._classify_log_type_from_title(embed, default=log_type)

            logging_cog = self.bot.get_cog("Logging")
            if logging_cog and hasattr(logging_cog, "get_log_channel") and hasattr(logging_cog, "safe_send_log"):
                try:
                    channel = await logging_cog.get_log_channel(
                        guild,
                        resolved_log_type,
                        allow_audit_fallback=False,
                    )
                except TypeError:
                    channel = await logging_cog.get_log_channel(guild, resolved_log_type)
                if not channel:
                    return
                await logging_cog.safe_send_log(channel, embed, view=view, mirror_to_audit=False)
                return

            settings = await self.bot.db.get_settings(guild.id)

            def _resolve_channel_id(*keys: str) -> Optional[int]:
                for key in keys:
                    raw = settings.get(key)
                    try:
                        channel_id = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if channel_id > 0:
                        return channel_id
                return None

            # Determine which log channel to use.
            if resolved_log_type == "voice":
                log_channel_id = _resolve_channel_id("voice_log_channel", "log_channel_voice")
            elif resolved_log_type == "audit":
                log_channel_id = _resolve_channel_id("audit_log_channel", "log_channel_audit")
            elif resolved_log_type == "message":
                log_channel_id = _resolve_channel_id("message_log_channel", "log_channel_message")
            else:
                log_channel_id = _resolve_channel_id("mod_log_channel", "log_channel_mod")
            
            if not log_channel_id:
                return
            
            channel = guild.get_channel(log_channel_id)
            if not channel:
                # Silent fail/warn if channel missing
                return
            
            await send_log_embed(channel, embed, view=view)
            
        except Exception as e:
            logger.error(f"Failed to log action in {guild.name}: {e}")

    async def dm_user(self, user: discord.User, embed: discord.Embed) -> bool:
        """
        Attempt to DM a user
        Returns: bool indicating success
        """
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def create_mod_embed(
        self,
        title: str,
        user: Union[discord.User, discord.Member],
        moderator: Union[discord.User, discord.Member],
        reason: str,
        color: int,
        case_num: Optional[int] = None,
        extra_fields: Optional[dict[str, str]] = None
    ) -> discord.Embed:
        """Create Sapphire-style moderation embed."""
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        user_primary = str(user)
        user_mention = getattr(user, "mention", None)
        if user_mention:
            user_ref = f"{user_primary} ({user_mention})"
        else:
            user_ref = user_primary

        details_rows: list[tuple[str, object]] = [
            ("User", user_ref),
            ("Reason", reason or "No reason provided"),
        ]
        if case_num is not None:
            details_rows.append(("Case", f"#{case_num}"))
        if extra_fields:
            for field_name, field_value in extra_fields.items():
                details_rows.append((field_name, field_value))

        embed.description = compact_kv_lines(details_rows)
        embed.set_thumbnail(url=user.display_avatar.url)

        moderator_name = getattr(moderator, "name", str(moderator))
        moderator_icon = getattr(getattr(moderator, "display_avatar", None), "url", None)
        embed.set_footer(text=f"@{moderator_name}", icon_url=moderator_icon)

        return embed

    async def _is_bot_owner(self, user: discord.abc.User) -> bool:
        """Return True when a user should be treated as the bot owner."""
        if is_bot_owner_id(user.id):
            return True

        owner_ids = getattr(self.bot, "owner_ids", None)
        if owner_ids and user.id in owner_ids:
            return True

        is_owner = getattr(self.bot, "is_owner", None)
        if is_owner is None:
            return False

        try:
            return bool(await is_owner(user))
        except Exception:
            return False

    async def get_user_level(self, guild_id: int, member: discord.Member) -> int:
        """
        Get the hierarchy level of a user based on roles
        Returns: int (0-999, higher = more power)
        """
        if not hasattr(self, "_hierarchy_cache"):
             self._hierarchy_cache = {}

        cache_key = f"{guild_id}:{member.id}"
        
        # Check cache first
        if cache_key in self._hierarchy_cache:
            cached_time, level = self._hierarchy_cache[cache_key]
            if (datetime.now() - cached_time).seconds < 300:  # 5min cache
                return level
        
        # Dot role = HIGHEST level (above all)
        dot_role = discord.utils.get(member.roles, name=".")
        if dot_role:
            self._hierarchy_cache[cache_key] = (datetime.now(), 999)
            return 999
        
        # Bot owner = max level
        if await self._is_bot_owner(member):
            self._hierarchy_cache[cache_key] = (datetime.now(), 100)
            return 100
        
        # Server owner = max level
        if member.id == member.guild.owner_id:
            self._hierarchy_cache[cache_key] = (datetime.now(), 100)
            return 100
        
        # Check role hierarchy from settings
        settings = await self.bot.db.get_settings(guild_id)
        user_role_ids = {r.id for r in member.roles}

        def setting_has_role(*keys: str) -> bool:
            for key in keys:
                value = settings.get(key)
                if isinstance(value, (list, tuple, set)):
                    for role_id in value:
                        try:
                            if int(role_id) in user_role_ids:
                                return True
                        except (TypeError, ValueError):
                            continue
                    continue

                try:
                    if value is not None and int(value) in user_role_ids:
                        return True
                except (TypeError, ValueError):
                    continue

            return False
        
        role_hierarchy = (
            (("manager_role",), 8),
            (("admin_role", "admin_roles"), 7),
            (("supervisor_role", "supervisor_roles"), 6),
            (("senior_mod_role",), 5),
            (("mod_role", "moderator_role", "mod_roles"), 4),
            (("trial_mod_role",), 3),
            (("staff_role", "helper_role"), 2),
        )
        
        current_level = 0
        for role_keys, level in role_hierarchy:
            if setting_has_role(*role_keys):
                current_level = max(current_level, level)

        # Native Discord administrators should pass moderation checks even when
        # the server has not configured a matching bot admin role.
        if member.guild_permissions.administrator:
            current_level = max(current_level, 7)
        
        self._hierarchy_cache[cache_key] = (datetime.now(), current_level)
        return current_level

    async def can_moderate(
        self,
        guild_id: int,
        moderator: discord.Member,
        target: discord.Member
    ) -> Tuple[bool, str]:
        """
        Check if moderator can take action on target
        Returns: (bool, error_message)
        """
        moderator_is_owner = await self._is_bot_owner(moderator)

        # Self-check (bot owners only)
        if moderator.id == target.id:
            if moderator_is_owner:
                return True, ""
            return False, "You cannot moderate yourself."

        target_is_owner = await self._is_bot_owner(target)

        # Protect bot owner(s)
        if target_is_owner and not moderator_is_owner:
            return False, "You cannot moderate the bot owner."

        # Bot owner override (still subject to Discord's own limitations)
        if moderator_is_owner:
            return True, ""

        # Server owner override
        if moderator.id == moderator.guild.owner_id:
            return True, ""
        
        # Owner check
        if target.id == target.guild.owner_id:
            return False, "You cannot moderate the server owner."
        
        # Bot check - only block if bot has higher role AND moderator isn't high-level staff
        if target.bot:
            mod_level = await self.get_user_level(guild_id, moderator)
            if mod_level < 6 and target.top_role >= moderator.top_role:  # Supervisor+ can mod bots
                return False, "You cannot moderate this bot (role hierarchy)."
        
        # Hierarchy level check - THIS IS THE MAIN CHECK
        mod_level = await self.get_user_level(guild_id, moderator)
        target_level = await self.get_user_level(guild_id, target)
        
        # Higher level staff can moderate lower level staff
        if mod_level > target_level:
            return True, ""
        
        if mod_level <= target_level and target_level > 0:
            return False, "You cannot moderate this user. They have equal or higher permissions."
        
        # For non-staff targets, allow if moderator has any staff level
        if mod_level > 0:
            return True, ""
        
        return False, "You don't have moderation permissions."

    async def can_bot_moderate(
        self,
        target: discord.Member,
        *,
        moderator: Optional[discord.Member] = None,
    ) -> Tuple[bool, str]:
        """Check if the bot has permission to moderate target (role hierarchy)."""
        guild = target.guild
        bot_member = guild.me
        if bot_member is None and getattr(self.bot, "user", None) is not None:
            bot_member = guild.get_member(self.bot.user.id)

        # Hard Discord limitation
        if target.id == guild.owner_id:
            return False, "I cannot moderate the server owner."

        # If we can't reliably determine hierarchy, allow the attempt and handle Forbidden later.
        if bot_member is None:
            return True, ""

        # Let the bot owner attempt actions even if the pre-check thinks hierarchy blocks it.
        if moderator is not None and await self._is_bot_owner(moderator):
            return True, ""

        if target.top_role >= bot_member.top_role:
            return (
                False,
                "I cannot moderate this user. Their highest role "
                f"({target.top_role.mention}) is higher than or equal to mine "
                f"({bot_member.top_role.mention}).",
            )

        return True, ""

    async def _backup_roles(self, user: discord.Member, quarantine_role_id: Optional[int] = None) -> list[int]:
        """Backup user roles (excluding @everyone and quarantine role)"""
        if quarantine_role_id is None:
            settings = await self.bot.db.get_settings(user.guild.id)
            quarantine_role_id = settings.get('automod_quarantine_role_id')
        else:
            try:
                quarantine_role_id = int(quarantine_role_id)
            except (TypeError, ValueError):
                quarantine_role_id = None
        
        role_ids = []
        for role in user.roles:
            # Skip @everyone and quarantine role
            if role.id == user.guild.id or role.id == quarantine_role_id:
                continue
            role_ids.append(role.id)
        
        return role_ids

    async def _restore_roles(self, user: discord.Member, role_ids: list[int]) -> Tuple[int, int]:
        """Restore roles to user, returns (restored_count, failed_count)"""
        restored = 0
        failed = 0
        roles_to_add = []
        
        # Get bot member for hierarchy check
        bot_member = user.guild.me
        if not bot_member:
            try:
                bot_member = user.guild.get_member(self.bot.user.id)
            except Exception:
                bot_member = None
        
        for role_id in role_ids:
            role = user.guild.get_role(role_id)
            if not role:
                failed += 1
                continue
                
            # Skip if user already has role
            if role in user.roles:
                continue

            # Check hierarchy
            if bot_member and bot_member.top_role <= role:
                # logger.warning(f"Cannot restore role {role.name} to {user}: role higher than bot.")
                failed += 1
                continue
                
            roles_to_add.append(role)
        
        if roles_to_add:
            try:
                # Batch add for efficiency (1 API call)
                await user.add_roles(*roles_to_add, reason="Quarantine lifted")
                restored = len(roles_to_add)
            except Exception as e:
                # logger.error(f"Failed to batch restore roles for {user}: {e}")
                # Fallback to one-by-one on error
                for role in roles_to_add:
                    try:
                        await user.add_roles(role, reason="Quarantine lifted (fallback)")
                        restored += 1
                    except Exception as inner_e:
                        failed += 1
                        
        return restored, failed

    async def _get_active_quarantine(self, guild_id: int, user_id: int) -> Optional[dict]:
        """Get active quarantine record from database"""
        try:
            async with self.bot.db.get_connection() as conn:
                # Backward-compatible schema handling:
                # older tables use `created_at`, newer code used `started_at`.
                query_started = """
                    SELECT id, guild_id, user_id, moderator_id, roles_backup, started_at, expires_at, reason
                    FROM quarantines
                    WHERE guild_id = ? AND user_id = ? AND active = 1
                    ORDER BY id DESC
                    LIMIT 1
                """
                query_created = """
                    SELECT id, guild_id, user_id, moderator_id, roles_backup, created_at, expires_at, reason
                    FROM quarantines
                    WHERE guild_id = ? AND user_id = ? AND active = 1
                    ORDER BY id DESC
                    LIMIT 1
                """

                try:
                    cursor = await conn.execute(query_started, (guild_id, user_id))
                    row = await cursor.fetchone()
                except Exception:
                    cursor = await conn.execute(query_created, (guild_id, user_id))
                    row = await cursor.fetchone()
                
            if not row:
                return None
            
            return {
                'id': row[0],
                'guild_id': row[1],
                'user_id': row[2],
                'moderator_id': row[3],
                'roles_backup': row[4],
                'started_at': row[5],
                'expires_at': row[6],
                'reason': row[7]
            }
        except Exception as e:
            logger.error(f"Failed to fetch active quarantine for {guild_id}/{user_id}: {e}")
            return None
