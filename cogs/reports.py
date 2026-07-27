"""
Report System - User reporting
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from typing import Optional
from utils.embeds import ModEmbed
from utils.checks import is_mod
from utils.logging import send_log_embed
from config import Config

class Reports(commands.Cog):
    """User report management system"""
    
    # Command group for reports
    report_group = app_commands.Group(name="report", description="📝 Report management")
    
    def __init__(self, bot):
        self.bot = bot
    
    @report_group.command(name="user", description="📝 Report a user to the moderation team")
    @app_commands.describe(user="The user to report", reason="Reason for the report")
    async def report_user(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        if user.id == interaction.user.id:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Cannot Report", "You cannot report yourself."),
                ephemeral=True
            )
        
        if user.bot:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Cannot Report", "You cannot report bots."),
                ephemeral=True
            )
        
        report_id = await self.bot.db.create_report(
            interaction.guild_id, interaction.user.id, user.id, reason
        )
        
        # Send to report log channel, falling back to audit logs in compact setups
        settings = await self.bot.db.get_settings(interaction.guild_id)
        report_channel_id = settings.get('report_log_channel') or settings.get('audit_log_channel') or settings.get('log_channel_audit')
        if report_channel_id:
            channel = interaction.guild.get_channel(report_channel_id)
            if channel:
                embed = discord.Embed(
                    title=f"📝 New Report #{report_id}",
                    color=Config.COLOR_WARNING,
                    timestamp=datetime.utcnow()
                )
                embed.add_field(name="Reported User", value=f"{user.mention} ({user})", inline=True)
                embed.add_field(name="Reporter", value=f"{interaction.user.mention}", inline=True)
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.set_thumbnail(url=user.display_avatar.url)
                embed.set_footer(text=f"Report ID: {report_id} | Use /report resolve {report_id} to resolve")
                
                await send_log_embed(channel, embed)
        
        embed = ModEmbed.success(
            "Report Submitted",
            f"Your report against {user.mention} has been submitted.\n**Report ID:** #{report_id}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @report_group.command(name="list", description="📋 View reports for a user or all unresolved reports")
    @app_commands.describe(user="User to view reports for (leave empty for all unresolved)")
    @is_mod()
    async def report_list(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        if user:
            reports = await self.bot.db.get_reports(interaction.guild_id, user.id)
            title = f"📋 Reports for {user}"
        else:
            reports = await self.bot.db.get_reports(interaction.guild_id, resolved=False)
            title = "📋 Unresolved Reports"
        
        if not reports:
            return await interaction.response.send_message(
                embed=ModEmbed.info("No Reports", "No reports found."),
                ephemeral=True
            )
        
        embed = discord.Embed(title=title, color=Config.COLOR_INFO)
        
        for report in reports[:15]:
            reporter = interaction.guild.get_member(report['reporter_id'])
            reported = interaction.guild.get_member(report['reported_id'])
            
            reporter_name = reporter.name if reporter else f"ID: {report['reporter_id']}"
            reported_name = reported.mention if reported else f"ID: {report['reported_id']}"
            
            status = "✅ Resolved" if report['resolved'] else "⏳ Pending"
            
            embed.add_field(
                name=f"#{report['id']} - {status}",
                value=f"**Reported:** {reported_name}\n**By:** {reporter_name}\n**Reason:** {report['reason'][:100]}",
                inline=False
            )
        
        if len(reports) > 15:
            embed.set_footer(text=f"Showing 15 of {len(reports)} reports")
        
        await interaction.response.send_message(embed=embed)
    
    @report_group.command(name="resolve", description="✅ Resolve a report")
    @app_commands.describe(report_id="The report ID to resolve")
    @is_mod()
    async def report_resolve(self, interaction: discord.Interaction, report_id: int):
        success = await self.bot.db.resolve_report(
            interaction.guild_id, report_id, interaction.user.id
        )
        
        if success:
            embed = ModEmbed.success("Report Resolved", f"Report #{report_id} has been marked as resolved.")
            
            # Log the resolution, falling back to audit logs in compact setups
            settings = await self.bot.db.get_settings(interaction.guild_id)
            report_channel_id = settings.get('report_log_channel') or settings.get('audit_log_channel') or settings.get('log_channel_audit')
            if report_channel_id:
                channel = interaction.guild.get_channel(report_channel_id)
                if channel:
                    log_embed = discord.Embed(
                        title=f"✅ Report #{report_id} Resolved",
                        description=f"Resolved by {interaction.user.mention}",
                        color=Config.COLOR_SUCCESS,
                        timestamp=datetime.utcnow()
                    )
                    await send_log_embed(channel, log_embed)
        else:
            embed = ModEmbed.error("Not Found", f"Report #{report_id} was not found.")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Reports(bot))

