import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional

from utils.embeds import ModEmbed, Colors
from utils.checks import is_mod, is_bot_owner_id
from utils.warning_escalation import apply_warning_escalation

class WarningCommands:
    async def _warn_logic(self, source, user: discord.Member, reason: str):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        can_mod, error = await self.can_moderate(
            source.guild.id,
            author,
            user,
            allow_protected_target=True,
        )
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Warn", error), ephemeral=True)
        
        settings = await self.bot.db.get_settings(source.guild.id)
        existing_warnings = await self.bot.db.get_warnings(source.guild.id, user.id)
        projected_warning_count = len(existing_warnings) + 1
        case_num = await self.bot.db.create_case(
            source.guild.id, user.id, author.id, "Warn", reason
        )
        dm_embed = discord.Embed(
            title=f"⚠️ Warning in {source.guild.name}",
            description=f"**Reason:** {reason}\n**Total Warnings:** {projected_warning_count}",
            color=Colors.WARNING
        )
        if settings.get("moderation_dm_users", True):
            await self.send_punishment_notice(
                guild=source.guild,
                user=user,
                action="Warn",
                reason=reason,
                case_number=case_num,
                settings=settings,
                fallback_embed=dm_embed,
            )

        # Record the warning only after the member notification is attempted.
        warning_id, warn_count = await self.bot.db.add_warning(source.guild.id, user.id, author.id, reason)
        warnings = await self.bot.db.get_warnings(source.guild.id, user.id)

        # Create embed
        embed = await self.create_mod_embed(
            title="⚠️ User Warned",
            user=user,
            moderator=author,
            reason=reason,
            color=Colors.WARNING,
            case_num=case_num,
            extra_fields={"Total Warnings": str(len(warnings))}
        )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)
        
        # Apply only the highest escalation whose threshold this warning crossed.
        auto_action = None

        async def create_escalation_case(action: str, action_reason: str, duration_seconds: Optional[int]):
            duration = f"{duration_seconds // 60}m" if duration_seconds else None
            await self.bot.db.create_case(
                source.guild.id,
                user.id,
                self.bot.user.id,
                action,
                action_reason,
                duration,
            )

        try:
            escalation = await apply_warning_escalation(
                source.guild,
                user,
                settings,
                warn_count,
                previous_count=max(0, warn_count - 1),
                reason_prefix="Automatic warning escalation",
                ban_delete_days=1,
                create_case=create_escalation_case,
            )
            if escalation is not None:
                action_labels = {
                    "timeout": "Auto-timed out",
                    "kick": "Auto-kicked",
                    "ban": "Auto-banned",
                }
                duration = ""
                if escalation.rule.duration_seconds:
                    duration = f" for {escalation.rule.duration_seconds // 60}m"
                auto_action = (
                    f"**{action_labels[escalation.rule.action]}** {user.mention}{duration} "
                    f"— reached {warn_count} warnings"
                )
        except discord.Forbidden:
            auto_action = f"Auto-punishment failed for {user.mention}: missing permissions"
        except discord.HTTPException as exc:
            auto_action = f"Auto-punishment failed for {user.mention}: {exc}"

        if auto_action:
            auto_embed = discord.Embed(description=auto_action, color=Colors.ERROR, timestamp=datetime.now(timezone.utc))
            await self._respond(source, embed=auto_embed)

    async def _warnings_logic(self, source, user: discord.Member):
        warnings = await self.bot.db.get_warnings(source.guild.id, user.id)
        
        if not warnings:
            return await self._respond(source, embed=ModEmbed.info("No Warnings", f"{user.mention} has no warnings."), ephemeral=True)
        
        embed = discord.Embed(
            title=f"⚠️ Warnings for {user.display_name}",
            description=f"Total: **{len(warnings)}** warning(s)",
            color=Colors.WARNING,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        for warn in warnings[:10]:  # Limit to 10 most recent
            moderator = source.guild.get_member(warn['moderator_id'])
            mod_display = moderator.display_name if moderator else f"ID: {warn['moderator_id']}"
            timestamp = warn.get('created_at', 'Unknown time')
            
            embed.add_field(
                name=f"Warning #{warn['id']}",
                value=f"**Reason:** {warn['reason'][:100]}\n**By:** {mod_display}\n**When:** {timestamp}",
                inline=False
            )
        
        if len(warnings) > 10:
            embed.set_footer(text=f"Showing 10 of {len(warnings)} warnings")
        
        await self._respond(source, embed=embed)

    async def _delwarn_logic(self, source, warning_id: int):
        success = await self.bot.db.delete_warning(source.guild.id, warning_id)
        
        if success:
            embed = ModEmbed.success("Warning Deleted", f"Warning `#{warning_id}` has been removed.")
        else:
            embed = ModEmbed.error("Not Found", f"Warning `#{warning_id}` does not exist.")
        
        await self._respond(source, embed=embed, ephemeral=True)

    async def _clearwarnings_logic(self, source, user: discord.Member, reason: str):
        count = await self.bot.db.clear_warnings(source.guild.id, user.id)
        
        embed = ModEmbed.success(
            "Warnings Cleared",
            f"Cleared **{count}** warning(s) from {user.mention}.\n**Reason:** {reason}"
        )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

    # Commands
    @commands.command(name="warn", description="⚠️ Warn a user")
    @is_mod()
    async def mod_warn(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
        await self._warn_logic(ctx, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def warn_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await self._warn_logic(interaction, user, reason)

    @commands.command(name="warnings", description="⚠️ View warnings for a user")
    @is_mod()
    async def mod_warnings(self, ctx: commands.Context, user: discord.Member):
        await self._warnings_logic(ctx, user)

    # Slash command - registered dynamically in __init__.py
    async def warnings_slash(self, interaction: discord.Interaction, user: discord.Member):
        await self._warnings_logic(interaction, user)

    @commands.command(name="delwarn", description="🗑️ Delete a warning")
    @is_mod()
    async def mod_delwarn(self, ctx: commands.Context, warning_id: int):
        await self._delwarn_logic(ctx, warning_id)

    # Slash command - registered dynamically in __init__.py
    async def delwarn_slash(self, interaction: discord.Interaction, warning_id: int):
        await self._delwarn_logic(interaction, warning_id)

    @commands.command(name="clearwarnings", description="🧹 Clear all warnings for a user")
    @is_mod()
    async def mod_clearwarnings(self, ctx: commands.Context, user: discord.Member, *, reason: str = "Cleared by moderator"):
        await self._clearwarnings_logic(ctx, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def clearwarnings_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Cleared by moderator"):
        await self._clearwarnings_logic(interaction, user, reason)
