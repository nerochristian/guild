"""
Server Backup/Restore System

Snapshots server structure (roles, channels, categories, permissions,
automod settings, rules) and restores on demand. Auto-backups before
dangerous owner-level actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.checks import is_admin, is_bot_owner_id, get_owner_ids
from utils.embeds import ModEmbed

logger = logging.getLogger("ModBot.ServerBackup")

CONFIRM_TIMEOUT = 60
MAX_BACKUPS_PER_GUILD = 25


def _serialise_overwrites(overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite]) -> Dict[str, Any]:
    """Convert channel permission overwrites to JSON-safe dict."""
    out = {}
    for target, overwrite in overwrites.items():
        key = f"r_{target.id}" if isinstance(target, discord.Role) else f"u_{target.id}"
        out[key] = {
            "allow": overwrite.pair()[0].value,
            "deny": overwrite.pair()[1].value,
        }
    return out


class ServerBackup(commands.Cog):
    """Snapshot and restore server structure."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # =========================================================================
    # Slash commands
    # =========================================================================

    server_backup = app_commands.Group(
        name="backup",
        description="Server backup and restore",
        default_permissions=discord.Permissions(administrator=True),
    )

    @server_backup.command(name="create")
    @app_commands.describe(label="Optional label for this backup")
    async def backup_create(self, interaction: discord.Interaction, label: Optional[str] = None) -> None:
        """Take a snapshot of the server structure."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not await self._check_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        snap = await self._snapshot_guild(guild)
        summary = _build_summary(snap, label)

        backup_id = await interaction.client.db.create_server_backup(
            guild_id=guild.id,
            created_by=interaction.user.id,
            backup_data=snap,
            triggered_by=label or "manual",
            summary=summary,
        )

        await interaction.followup.send(
            embed=ModEmbed.success(
                title="Server Backup Created",
                description=f"Backup **#{backup_id}** saved.\n{summary}",
            ),
            ephemeral=True,
        )
        logger.info("Backup #%d created for guild %d by %d", backup_id, guild.id, interaction.user.id)

    @server_backup.command(name="list")
    async def backup_list(self, interaction: discord.Interaction) -> None:
        """Show recent server backups."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not await self._check_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        backups = await interaction.client.db.list_server_backups(guild.id, limit=10)
        if not backups:
            await interaction.followup.send(
                embed=ModEmbed.info(title="No Backups", description="No server backups found."),
                ephemeral=True,
            )
            return

        lines = []
        for b in backups:
            ts = b["created_at"]
            lines.append(f"**#{b['id']}** — {b['triggered_by']} — <t:{_to_unix(ts)}:R>")
            if b["summary"]:
                lines[-1] += f"\n> {b['summary']}"

        embed = ModEmbed.info(
            title="Server Backups",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @server_backup.command(name="preview")
    @app_commands.describe(backup_id="Backup number to preview")
    async def backup_preview(self, interaction: discord.Interaction, backup_id: int) -> None:
        """Preview what's inside a backup."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not await self._check_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        backup = await interaction.client.db.get_server_backup(backup_id)
        if backup is None or backup.get("guild_id") != guild.id:
            await interaction.followup.send(
                embed=ModEmbed.error(title="Not Found", description=f"Backup #{backup_id} not found."),
                ephemeral=True,
            )
            return

        snap = backup["backup_data"]
        details = _build_preview_lines(snap)
        embed = ModEmbed.info(
            title=f"Backup #{backup_id} Preview",
            description="\n".join(details),
        )
        if backup.get("created_at"):
            embed.set_footer(text=f"Created {backup['created_at']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @server_backup.command(name="restore")
    @app_commands.describe(backup_id="Backup number to restore", confirm="Type CONFIRM to proceed")
    async def backup_restore(self, interaction: discord.Interaction, backup_id: int, confirm: Optional[str] = None) -> None:
        """Restore server structure from a backup. Requires confirmation."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not await self._check_perms(interaction):
            return

        backup = await interaction.client.db.get_server_backup(backup_id)
        if backup is None or backup.get("guild_id") != guild.id:
            await interaction.response.send_message(
                embed=ModEmbed.error(title="Not Found", description=f"Backup #{backup_id} not found."),
                ephemeral=True,
            )
            return

        snap = backup["backup_data"]

        if confirm is None:
            preview = _build_preview_lines(snap)
            embed = ModEmbed.warning(
                title="⚠️ Confirm Restore",
                description=(
                    f"You are about to restore **Backup #{backup_id}**.\n\n"
                    + "\n".join(preview)
                    + f"\n\nRun this command again with `confirm:CONFIRM` to proceed."
                ),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if confirm.strip().upper() != "CONFIRM":
            await interaction.response.send_message(
                embed=ModEmbed.error(
                    title="Invalid Confirmation",
                    description="Please type `CONFIRM` as the confirm argument.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        results = await self._restore_snapshot(guild, snap)
        embed = ModEmbed.success(
            title="Server Restored",
            description="\n".join(results),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("Backup #%d restored for guild %d by %d", backup_id, guild.id, interaction.user.id)

    # =========================================================================
    # Snapshot logic
    # =========================================================================

    async def _snapshot_guild(self, guild: discord.Guild) -> Dict[str, Any]:
        """Capture current server structure."""
        roles = []
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            if role.is_default() or role.managed:
                continue
            roles.append({
                "name": role.name,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "permissions": role.permissions.value,
                "position": role.position,
            })

        categories = {}
        channels = []

        for channel in guild.channels:
            overwrites = _serialise_overwrites(channel.overwrites)
            ch_data = {
                "name": channel.name,
                "type": channel.type.value if hasattr(channel, "type") else 0,
                "position": channel.position,
                "overwrites": overwrites,
            }

            if isinstance(channel, discord.TextChannel):
                ch_data["topic"] = channel.topic or ""
                ch_data["slowmode_delay"] = channel.slowmode_delay
                ch_data["nsfw"] = channel.nsfw
            elif isinstance(channel, discord.VoiceChannel):
                ch_data["bitrate"] = channel.bitrate
                ch_data["user_limit"] = channel.user_limit

            if isinstance(channel, discord.CategoryChannel):
                categories[str(channel.id)] = ch_data
            else:
                if channel.category_id is not None:
                    ch_data["category_name"] = self._get_category_name(guild, channel.category_id)
                channels.append(ch_data)

        automod_settings = await self.bot.db.get_settings(guild.id)
        automod_keys = {k: v for k, v in automod_settings.items() if k.startswith("automod_")}

        return {
            "roles": roles,
            "categories": list(categories.values()),
            "channels": channels,
            "automod": automod_keys,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
        }

    def _get_category_name(self, guild: discord.Guild, category_id: int) -> str:
        cat = guild.get_channel(category_id)
        return cat.name if cat else "unknown"

    # =========================================================================
    # Restore logic
    # =========================================================================

    async def _restore_snapshot(self, guild: discord.Guild, snap: Dict[str, Any]) -> List[str]:
        """Restore roles, channels, and automod settings from snapshot."""
        results = []

        # --- Restore roles ---
        existing_roles = {r.name: r for r in guild.roles}
        for role_data in snap.get("roles", []):
            existing = existing_roles.get(role_data["name"])
            if existing and not existing.managed and not existing.is_default():
                try:
                    await existing.edit(
                        color=discord.Color(role_data.get("color", 0)),
                        hoist=role_data.get("hoist", False),
                        mentionable=role_data.get("mentionable", False),
                        permissions=discord.Permissions(role_data.get("permissions", 0)),
                        reason="Server backup restore",
                    )
                except discord.HTTPException:
                    pass
            elif not existing:
                try:
                    await guild.create_role(
                        name=role_data["name"],
                        color=discord.Color(role_data.get("color", 0)),
                        hoist=role_data.get("hoist", False),
                        mentionable=role_data.get("mentionable", False),
                        permissions=discord.Permissions(role_data.get("permissions", 0)),
                        reason="Server backup restore",
                    )
                except discord.HTTPException:
                    pass
        results.append(f"✅ Roles: {len(snap.get('roles', []))} processed")

        # --- Restore categories ---
        existing_cats = {c.name: c for c in guild.categories}
        cat_id_map: Dict[str, discord.CategoryChannel] = {}
        new_cats = 0
        for cat_data in snap.get("categories", []):
            existing = existing_cats.get(cat_data["name"])
            if existing:
                cat_id_map[cat_data["name"]] = existing
            else:
                try:
                    new_cat = await guild.create_category(
                        name=cat_data["name"],
                        position=cat_data.get("position", 0),
                        reason="Server backup restore",
                    )
                    cat_id_map[cat_data["name"]] = new_cat
                    new_cats += 1
                except discord.HTTPException:
                    pass
        results.append(f"📁 Categories: {len(cat_id_map)} mapped ({new_cats} new)")

        # --- Restore channels ---
        role_lookup = {r.name: r for r in guild.roles}
        existing_channels = {c.name: c for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
        restored = 0
        for ch_data in snap.get("channels", []):
            category = cat_id_map.get(ch_data.get("category_name")) if ch_data.get("category_name") else None
            existing = existing_channels.get(ch_data["name"])

            if existing:
                try:
                    await existing.edit(
                        category=category,
                        position=ch_data.get("position", existing.position),
                        reason="Server backup restore",
                    )
                    if isinstance(existing, discord.TextChannel):
                        await existing.edit(
                            topic=ch_data.get("topic", ""),
                            slowmode_delay=ch_data.get("slowmode_delay", 0),
                            nsfw=ch_data.get("nsfw", False),
                        )
                    restored += 1
                except discord.HTTPException:
                    pass
            else:
                try:
                    ch_type = discord.ChannelType(ch_data.get("type", 0))
                    if ch_type == discord.ChannelType.text:
                        new_ch = await guild.create_text_channel(
                            name=ch_data["name"],
                            category=category,
                            position=ch_data.get("position", 0),
                            topic=ch_data.get("topic", ""),
                            slowmode_delay=ch_data.get("slowmode_delay", 0),
                            nsfw=ch_data.get("nsfw", False),
                            reason="Server backup restore",
                        )
                    elif ch_type == discord.ChannelType.voice:
                        new_ch = await guild.create_voice_channel(
                            name=ch_data["name"],
                            category=category,
                            position=ch_data.get("position", 0),
                            bitrate=ch_data.get("bitrate", 64000),
                            user_limit=ch_data.get("user_limit", 0),
                            reason="Server backup restore",
                        )
                    else:
                        continue
                    restored += 1
                except discord.HTTPException:
                    pass

        results.append(f"💬 Channels: {restored} restored")

        # --- Restore automod settings ---
        automod = snap.get("automod", {})
        if automod:
            await self.bot.db.update_settings(guild.id, automod)
            results.append(f"🛡️ AutoMod: {len(automod)} settings restored")

        return results

    # =========================================================================
    # Auto-backup trigger
    # =========================================================================

    async def auto_backup(self, guild: discord.Guild, triggered_by: str) -> Optional[int]:
        """Take an automatic backup before a dangerous action. Returns backup id or None."""
        try:
            snap = await self._snapshot_guild(guild)
            summary = f"Auto: {triggered_by}"
            backup_id = await self.bot.db.create_server_backup(
                guild_id=guild.id,
                created_by=self.bot.user.id,
                backup_data=snap,
                triggered_by=triggered_by,
                summary=summary,
            )
            await self.bot.db.prune_old_backups(guild.id, keep=MAX_BACKUPS_PER_GUILD)
            return backup_id
        except Exception:
            logger.exception("Auto-backup failed for guild %d (%s)", guild.id, triggered_by)
            return None

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _check_perms(self, interaction: discord.Interaction) -> bool:
        """Require admin or bot owner."""
        member = interaction.user
        if isinstance(member, discord.Member):
            if member.guild_permissions.administrator:
                return True
            if is_bot_owner_id(member.id):
                return True
        await interaction.response.send_message(
            embed=ModEmbed.error(
                title="Permission Denied",
                description="You need Administrator permission to manage server backups.",
            ),
            ephemeral=True,
        )
        return False


def _build_summary(snap: Dict[str, Any], label: Optional[str] = None) -> str:
    parts = [
        f"{len(snap.get('roles', []))} roles",
        f"{len(snap.get('categories', []))} categories",
        f"{len(snap.get('channels', []))} channels",
    ]
    automod = snap.get("automod", {})
    if automod:
        parts.append(f"{len(automod)} automod keys")
    prefix = f"{label} — " if label else ""
    return prefix + " • ".join(parts)


def _build_preview_lines(snap: Dict[str, Any]) -> List[str]:
    lines = []
    roles = snap.get("roles", [])
    if roles:
        lines.append(f"**👥 Roles** ({len(roles)}): {', '.join(r['name'] for r in roles[:8])}" + ("..." if len(roles) > 8 else ""))
    cats = snap.get("categories", [])
    if cats:
        lines.append(f"**📁 Categories** ({len(cats)}): {', '.join(c['name'] for c in cats[:5])}" + ("..." if len(cats) > 5 else ""))
    channels = snap.get("channels", [])
    if channels:
        lines.append(f"**💬 Channels** ({len(channels)}): {', '.join(c['name'] for c in channels[:10])}" + ("..." if len(channels) > 10 else ""))
    automod = snap.get("automod", {})
    if automod:
        enabled = [k.replace("automod_", "").replace("_", " ").title() for k, v in automod.items() if isinstance(v, bool) and v and k != "automod_enabled"]
        if enabled:
            lines.append(f"**🛡️ AutoMod**: {', '.join(enabled[:6])}")
    return lines


def _to_unix(ts_str: str) -> int:
    try:
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerBackup(bot))
