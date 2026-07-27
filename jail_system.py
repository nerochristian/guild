import asyncio
import logging
import os
from typing import Optional

import discord
import psycopg2
from discord import app_commands
from discord.ext import commands
from psycopg2.extras import Json

SUPER_USER_IDS = {1269772767516033025}

try:
    from safety_system import BotSafetyStore
except ModuleNotFoundError:
    logging.getLogger(__name__).warning("safety_system.py is missing.")

LOGGER = logging.getLogger(__name__)

ERROR_RED = 0xFF4A4A
SUCCESS_GREEN = 0x2ECC71
ACCENT_COLOR = 0x5865F2


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required; configure it before starting the bot.")
    return value


class JailStore:
    def __init__(self, db_url: str) -> None:
        if not db_url:
            raise RuntimeError(
                "DATABASE_URL is required; configure it before starting the bot."
            )
        self.db_url = db_url

    def _connect(self):
        return psycopg2.connect(self.db_url)

    def initialize(self) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS jailed_users (
                            guild_id BIGINT,
                            user_id BIGINT,
                            original_roles JSONB,
                            jailed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
        finally:
            conn.close()

    def jail_user(self, guild_id: int, user_id: int, role_ids: list[int]) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO jailed_users (guild_id, user_id, original_roles)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            original_roles = EXCLUDED.original_roles,
                            jailed_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, user_id, Json(role_ids)),
                    )
        finally:
            conn.close()

    def unjail_user(self, guild_id: int, user_id: int) -> Optional[list[int]]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT original_roles FROM jailed_users WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if row:
                        cursor.execute(
                            "DELETE FROM jailed_users WHERE guild_id = %s AND user_id = %s",
                            (guild_id, user_id),
                        )
                        return row[0]
                    return None
        finally:
            conn.close()

    def get_jailed_roles(self, guild_id: int, user_id: int) -> Optional[list[int]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT original_roles FROM jailed_users WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def clear_jailed_user(self, guild_id: int, user_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM jailed_users WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
        finally:
            conn.close()


class JailSystem:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = JailStore(require_env("DATABASE_URL"))
        self._configured_guilds: set[int] = set()

    async def _bot_enabled(self, guild_id: int) -> bool:
        safety_store = getattr(self.bot, "safety_store", None)
        if safety_store is None:
            return True
        try:
            return await asyncio.to_thread(safety_store.is_enabled, guild_id)
        except Exception:
            LOGGER.exception("Failed to read bot safety state")
            return False

    async def _ensure_jail_assets(
        self, guild: discord.Guild
    ) -> tuple[discord.Role, discord.TextChannel]:
        jailed_role = discord.utils.get(guild.roles, name="Jailed")
        jail_channel = discord.utils.get(guild.text_channels, name="jail")
        if (
            guild.id in self._configured_guilds
            and jailed_role is not None
            and jail_channel is not None
        ):
            return jailed_role, jail_channel

        if jailed_role is None:
            jailed_role = await guild.create_role(
                name="Jailed",
                color=discord.Color.darker_grey(),
                reason="Jail system setup",
            )
            for category in guild.categories:
                try:
                    await category.set_permissions(
                        jailed_role, view_channel=False, send_messages=False
                    )
                except discord.Forbidden:
                    pass
            for channel in guild.text_channels:
                try:
                    if channel.category is None:
                        await channel.set_permissions(
                            jailed_role, view_channel=False, send_messages=False
                        )
                except discord.Forbidden:
                    pass

        if jail_channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                jailed_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            }
            jail_channel = await guild.create_text_channel(
                "jail",
                overwrites=overwrites,
                topic="Quarantine Zone",
                position=0,
            )
        else:
            await jail_channel.set_permissions(
                guild.default_role,
                view_channel=False,
                send_messages=False,
                reason="Restrict jail channel",
            )
            await jail_channel.set_permissions(
                jailed_role,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

        self._configured_guilds.add(guild.id)
        return jailed_role, jail_channel

    def setup(self) -> None:
        self.store.initialize()

        @self.bot.tree.command(
            name="jail",
            description="Moderation: Quarantine a user and strip their roles",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            user="The user to jail", reason="The reason for jailing them"
        )
        async def jail_cmd(
            interaction: discord.Interaction,
            user: discord.Member,
            reason: Optional[str] = "No reason provided",
        ):
            member = interaction.user
            if not isinstance(member, discord.Member):
                return
            if not await self._bot_enabled(interaction.guild_id):
                await interaction.response.send_message("BOT IS OFF", ephemeral=True)
                return

            settings_store = getattr(self.bot, "guild_settings", None)
            settings = settings_store.get_settings(interaction.guild_id) if settings_store else {}
            bypass_role_id = settings.get("bypass_role_id")
            has_role = member.id in SUPER_USER_IDS or any(
                r.id == bypass_role_id
                or r.name.lower() in ("soul staff", "officer")
                for r in member.roles
            )
            has_perm = (
                member.guild_permissions.administrator
                or member.guild_permissions.manage_guild
            )
            if not has_role and not has_perm:
                await interaction.response.send_message(
                    "You don't have permission.", ephemeral=True
                )
                return

            if (
                user.top_role >= member.top_role
                and member.id != interaction.guild.owner_id
            ):
                await interaction.response.send_message(
                    "You cannot jail a member with an equal or higher role.",
                    ephemeral=True,
                )
                return

            if user.bot:
                await interaction.response.send_message(
                    "You cannot jail a bot.", ephemeral=True
                )
                return

            if interaction.channel is None:
                await interaction.response.send_message(
                    "This command needs a channel for officer approval.", ephemeral=True
                )
                return

            guild = interaction.guild

            jailed_role, jail_channel = await self._ensure_jail_assets(guild)

            if jailed_role in user.roles:
                await interaction.followup.send(f"{user.mention} is already in jail.")
                return

            original_role_ids = [
                r.id
                for r in user.roles
                if r != guild.default_role
                and not r.is_integration()
                and not r.is_premium_subscriber()
                and r.is_assignable()
            ]
            roles_to_remove = [
                role
                for role in (guild.get_role(r_id) for r_id in original_role_ids)
                if role is not None
            ]

            try:
                if roles_to_remove:
                    await user.remove_roles(
                        *roles_to_remove,
                        reason=f"Jailed by {member}; reason: {reason}",
                    )
                await user.add_roles(
                    jailed_role,
                    reason=f"Jailed by {member}; reason: {reason}",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "I don't have permission to modify this user's roles (my role might be lower than theirs)."
                )
                return
            except discord.HTTPException:
                LOGGER.exception(
                    "Failed to apply jail roles for %s in %s", user.id, guild.id
                )
                await interaction.followup.send(
                    "Discord rejected the role update. No jail record was saved."
                )
                return

            try:
                await asyncio.to_thread(
                    self.store.jail_user, guild.id, user.id, original_role_ids
                )
            except Exception:
                LOGGER.exception(
                    "Failed to save jail record for %s in %s", user.id, guild.id
                )
                try:
                    if jailed_role in user.roles:
                        await user.remove_roles(
                            jailed_role, reason="Rollback failed jail database write"
                        )
                    if roles_to_remove:
                        await user.add_roles(
                            *roles_to_remove,
                            reason="Rollback failed jail database write",
                        )
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.exception(
                        "Failed to roll back Discord jail roles for %s in %s",
                        user.id,
                        guild.id,
                    )
                await interaction.followup.send(
                    "I could not save the jail record, so the jail action was cancelled."
                )
                return

            # Send welcome message in #jail
            welcome_embed = discord.Embed(
                title="🚨 Welcome to Quarantine",
                description=(
                    f"Hello {user.mention}, you have been quarantined by {member.mention}.\n\n"
                    f"**Reason:** {reason}\n\n"
                    "You have lost access to the rest of the server until staff review your case. "
                    "Please wait patiently and do not leave the server, as attempting to bypass this quarantine may result in a ban."
                ),
                color=ERROR_RED,
            )
            try:
                await jail_channel.send(content=user.mention, embed=welcome_embed)
            except discord.Forbidden:
                pass

            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"Successfully jailed {user.mention} in {jail_channel.mention}.",
                    color=SUCCESS_GREEN,
                ),
                ephemeral=True,
            )

        @self.bot.tree.command(
            name="unjail",
            description="Moderation: Remove a user from quarantine and restore their roles",
        )
        @app_commands.guild_only()
        @app_commands.describe(user="The user to unjail")
        async def unjail_cmd(interaction: discord.Interaction, user: discord.Member):
            member = interaction.user
            if not isinstance(member, discord.Member):
                return
            if not await self._bot_enabled(interaction.guild_id):
                await interaction.response.send_message("BOT IS OFF", ephemeral=True)
                return

            settings_store = getattr(self.bot, "guild_settings", None)
            settings = settings_store.get_settings(interaction.guild_id) if settings_store else {}
            bypass_role_id = settings.get("bypass_role_id")
            has_role = member.id in SUPER_USER_IDS or any(
                r.id == bypass_role_id
                or r.name.lower() in ("soul staff", "officer")
                for r in member.roles
            )
            has_perm = (
                member.guild_permissions.administrator
                or member.guild_permissions.manage_guild
            )
            if not has_role and not has_perm:
                await interaction.response.send_message(
                    "You don't have permission.", ephemeral=True
                )
                return

            if interaction.channel is None:
                await interaction.response.send_message(
                    "This command needs a channel for officer approval.", ephemeral=True
                )
                return

            guild = interaction.guild

            jailed_role = discord.utils.get(guild.roles, name="Jailed")
            original_role_ids = await asyncio.to_thread(
                self.store.get_jailed_roles, guild.id, user.id
            )

            if original_role_ids is None and (
                not jailed_role or jailed_role not in user.roles
            ):
                await interaction.followup.send(f"{user.mention} is not in jail.")
                return

            roles_to_restore = []
            if original_role_ids:
                for r_id in original_role_ids:
                    role = guild.get_role(r_id)
                    if role:
                        roles_to_restore.append(role)

            try:
                if roles_to_restore:
                    await user.add_roles(
                        *roles_to_restore,
                        reason=f"Unjailed by {member}",
                    )
                if jailed_role and jailed_role in user.roles:
                    await user.remove_roles(
                        jailed_role,
                        reason=f"Unjailed by {member}",
                    )
            except discord.Forbidden:
                await interaction.followup.send(
                    "I don't have permission to modify this user's roles."
                )
                return
            except discord.HTTPException:
                LOGGER.exception(
                    "Failed to apply unjail roles for %s in %s", user.id, guild.id
                )
                await interaction.followup.send(
                    "Discord rejected the role update, so the jail record was left unchanged."
                )
                return

            try:
                await asyncio.to_thread(self.store.clear_jailed_user, guild.id, user.id)
            except Exception:
                LOGGER.exception(
                    "Failed to clear jail record for %s in %s", user.id, guild.id
                )
                await interaction.followup.send(
                    "Roles were updated, but I could not clear the jail database record. Try `/unjail` again."
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"Successfully unjailed {user.mention}.",
                    color=SUCCESS_GREEN,
                ),
                ephemeral=True,
            )


def init_jail_system(bot: commands.Bot) -> JailSystem:
    return JailSystem(bot)
