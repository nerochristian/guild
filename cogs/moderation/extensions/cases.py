import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional

from utils.embeds import ModEmbed, Colors
from utils.checks import is_mod

class CaseCommands:
    async def _case_logic(self, source, case_number: int):
        guild_id = source.guild.id
        case = await self.bot.db.get_case(guild_id, case_number)
        
        if not case:
            return await self._respond(source, embed=ModEmbed.error("Not Found", f"Case #{case_number} does not exist."))
        
        try:
            user = await self.bot.fetch_user(case['user_id'])
            moderator = await self.bot.fetch_user(case['moderator_id'])
        except discord.NotFound:
            user = f"Unknown User ({case['user_id']})"
            moderator = f"Unknown Moderator ({case['moderator_id']})"
        
        embed = discord.Embed(
            title=f"Case #{case['case_number']} - {case['action']}",
            color=Colors.MOD,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="User", value=f"{user.mention if hasattr(user, 'mention') else user}", inline=True)
        embed.add_field(name="Moderator", value=f"{moderator.mention if hasattr(moderator, 'mention') else moderator}", inline=True)
        embed.add_field(name="Reason", value=case['reason'], inline=False)
        
        if hasattr(user, 'display_avatar'):
            embed.set_thumbnail(url=user.display_avatar.url)
        
        await self._respond(source, embed=embed)

    async def _editcase_logic(self, source, case_number: int, reason: str):
        guild_id = source.guild.id
        case = await self.bot.db.get_case(guild_id, case_number)
        
        if not case:
             return await self._respond(source, embed=ModEmbed.error("Not Found", f"Case #{case_number} does not exist."))
        
        await self.bot.db.update_case(guild_id, case_number, reason)
        
        embed = ModEmbed.success(
            "Case Updated",
            f"Case #{case_number} reason has been updated to:\n`{reason}`"
        )
        
        await self._respond(source, embed=embed)

    async def _history_logic(self, source, user: discord.Member):
        guild = source.guild
        cases = await self.bot.db.get_user_cases(guild.id, user.id)
        
        if not cases:
             return await self._respond(source, embed=ModEmbed.info("No History", f"{user.mention} has no moderation history."))
        
        embed = discord.Embed(
            title=f"📜 Moderation History: {user.display_name}",
            description=f"Total cases: **{len(cases)}**",
            color=Colors.MOD,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        for case in cases[:10]:
            moderator = guild.get_member(case['moderator_id'])
            mod_display = moderator.display_name if moderator else f"ID: {case['moderator_id']}"
            
            embed.add_field(
                name=f"Case #{case['case_number']} - {case['action']}",
                value=f"**Reason:** {case['reason'][:100]}\n**By:** {mod_display}",
                inline=False
            )
        
        if len(cases) > 10:
            embed.set_footer(text=f"Showing 10 of {len(cases)} cases")
        
        await self._respond(source, embed=embed)

    async def _modlogs_logic(self, source, user: discord.Member):
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()
        
        guild = source.guild
        guild_id = guild.id
        
        # Fetch all data concurrently
        cases = await self.bot.db.get_user_cases(guild_id, user.id)
        warnings = await self.bot.db.get_warnings(guild_id, user.id)
        notes = await self.bot.db.get_notes(guild_id, user.id)
        
        all_logs = []
        
        # Process Cases
        for c in cases:
            all_logs.append({
                'type': 'case',
                'action': c['action'],
                'reason': c['reason'],
                'mod_id': c['moderator_id'],
                'timestamp': str(c['created_at']), # Ensure string for parsing
                'id': c['case_number']
            })
            
        # Process Warnings
        for w in warnings:
             all_logs.append({
                'type': 'warn',
                'action': 'Warning',
                'reason': w['reason'],
                'mod_id': w['moderator_id'],
                'timestamp': str(w['created_at']),
                'id': w['id']
            })
             
        # Process Notes
        for n in notes:
             all_logs.append({
                'type': 'note',
                'action': 'Note',
                'reason': n['note'],
                'mod_id': n['moderator_id'],
                'timestamp': str(n['created_at']),
                'id': n['id']
            })
        
        if not all_logs:
             return await self._respond(source, embed=ModEmbed.info("No Logs", f"{user.mention} has no moderation logs."))
            
        # Sort by timestamp (newest first)
        def parse_ts(x):
            try:
                # Try standard replace first
                return datetime.fromisoformat(str(x['timestamp']).replace(' ', 'T'))
            except:
                return datetime.min

        all_logs.sort(key=parse_ts, reverse=True)
        
        # Pagination Logic (Basic: Display first 15)
        embed = discord.Embed(
            title=f"📋 Mod Logs: {user.display_name}",
            color=Colors.MOD,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        description_lines = []
        
        for log in all_logs[:15]:
            # Emoji mapping
            emoji = "📝"
            if log['type'] == 'case':
                if 'ban' in log['action'].lower(): emoji = "🔨"
                elif 'kick' in log['action'].lower(): emoji = "👢"
                elif 'mute' in log['action'].lower(): emoji = "🔇"
                else: emoji = "🛡️"
            elif log['type'] == 'warn':
                emoji = "⚠️"
            
            # Timestamp formatting
            ts_obj = parse_ts(log)
            time_str = f"<t:{int(ts_obj.timestamp())}:R>" if ts_obj != datetime.min else "Unknown"
            
            mod_user = guild.get_member(log['mod_id'])
            mod_name = mod_user.name if mod_user else f"ID:{log['mod_id']}"
            
            # Format line
            # e.g. 🔨 **Ban** (#12) • 5m ago • _Spamming_ • by ModName
            line = f"{emoji} **{log['action'].title()}**"
            if log['type'] == 'case':
                line += f" (#{log['id']})"
            
            reason_short = (log['reason'] or "No reason")[:50]
            if len(log['reason'] or "") > 50: reason_short += "..."
                
            line += f" • {time_str} • *{reason_short}* • by **{mod_name}**"
            description_lines.append(line)
            
        embed.description = "\n".join(description_lines)
        
        if len(all_logs) > 15:
            embed.set_footer(text=f"Showing recent 15 of {len(all_logs)} entries")
        
        await self._respond(source, embed=embed)

    async def _note_logic(self, source, user: discord.Member, note: str):
        author_id = source.user.id if isinstance(source, discord.Interaction) else source.author.id
        await self.bot.db.add_note(source.guild.id, user.id, author_id, note)
        
        embed = ModEmbed.success(
            "Note Added",
            f"Added note to {user.mention}:\n`{note}`"
        )
        
        await self._respond(source, embed=embed)

    async def _notes_logic(self, source, user: discord.Member):
        guild = source.guild
        notes = await self.bot.db.get_notes(guild.id, user.id)
        
        if not notes:
             return await self._respond(source, embed=ModEmbed.info("No Notes", f"{user.mention} has no notes."))
        
        embed = discord.Embed(
            title=f"📋 Notes: {user.display_name}",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        for n in notes[:10]:
            moderator = guild.get_member(n['moderator_id'])
            mod_display = moderator.display_name if moderator else f"ID: {n['moderator_id']}"
            
            embed.add_field(
                name=f"Note #{n['id']}",
                value=f"**Content:** {n['content'][:100]}\n**By:** {mod_display}",
                inline=False
            )
        
        if len(notes) > 10:
            embed.set_footer(text=f"Showing 10 of {len(notes)} notes")
        
        await self._respond(source, embed=embed)

    async def _modstats_logic(self, source, moderator: Optional[discord.Member] = None):
        guild = source.guild
        if moderator:
            stats = await self.bot.db.get_moderator_stats(guild.id, moderator.id)
            
            embed = discord.Embed(
                title=f"📊 Mod Stats: {moderator.display_name}",
                color=Colors.MOD,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_thumbnail(url=moderator.display_avatar.url)
        else:
            stats = await self.bot.db.get_guild_mod_stats(guild.id)
            
            embed = discord.Embed(
                title=f"📊 Server Moderation Stats",
                color=Colors.MOD,
                timestamp=datetime.now(timezone.utc)
            )
        
        # Action counts
        action_fields = {
            "⚠️ Warnings": stats.get('warns', 0),
            "👢 Kicks": stats.get('kicks', 0),
            "🔨 Bans": stats.get('bans', 0),
            "⏰ Tempbans": stats.get('tempbans', 0),
            "🔇 Mutes": stats.get('mutes', 0),
            "🔒 Quarantines": stats.get('quarantines', 0)
        }
        
        for action, count in action_fields.items():
            embed.add_field(name=action, value=str(count), inline=True)
        
        total_actions = sum(action_fields.values())
        embed.add_field(name="📈 Total Actions", value=str(total_actions), inline=False)
        
        await self._respond(source, embed=embed)

    # Commands
    @commands.command(name="case", description="📋 View a specific moderation case")
    @is_mod()
    async def case(self, ctx: commands.Context, case_number: int):
        await self._case_logic(ctx, case_number)

    # Slash command - registered dynamically in __init__.py
    async def case_slash(self, interaction: discord.Interaction, case_number: int):
        await self._case_logic(interaction, case_number)

    @commands.command(name="editcase", description="✏️ Edit a case's reason")
    @is_mod()
    async def editcase(self, ctx: commands.Context, case_number: int, *, reason: str):
        await self._editcase_logic(ctx, case_number, reason)

    # Slash command - registered dynamically in __init__.py
    async def editcase_slash(self, interaction: discord.Interaction, case_number: int, reason: str):
        await self._editcase_logic(interaction, case_number, reason)

    @commands.command(name="history", description="📜 View a user's moderation history")
    @is_mod()
    async def history(self, ctx: commands.Context, user: discord.Member):
        await self._history_logic(ctx, user)

    # Slash command - registered dynamically in __init__.py
    async def history_slash(self, interaction: discord.Interaction, user: discord.Member):
        await self._history_logic(interaction, user)

    @commands.command(name="modlogs", description="📋 View comprehensive moderation logs")
    @is_mod()
    async def modlogs(self, ctx: commands.Context, user: discord.Member):
        await self._modlogs_logic(ctx, user)

    # Slash command - registered dynamically in __init__.py
    async def modlogs_slash(self, interaction: discord.Interaction, user: discord.Member):
        await self._modlogs_logic(interaction, user)

    @commands.command(name="note", description="📝 Add a note to a user")
    @is_mod()
    async def note(self, ctx: commands.Context, user: discord.Member, *, note: str):
        await self._note_logic(ctx, user, note)

    # Slash command - registered dynamically in __init__.py
    async def note_slash(self, interaction: discord.Interaction, user: discord.Member, note: str):
        await self._note_logic(interaction, user, note)

    @commands.command(name="notes", description="📋 View notes for a user")
    @is_mod()
    async def notes(self, ctx: commands.Context, user: discord.Member):
        await self._notes_logic(ctx, user)

    # Slash command - registered dynamically in __init__.py
    async def notes_slash(self, interaction: discord.Interaction, user: discord.Member):
        await self._notes_logic(interaction, user)

    @commands.command(name="modstats", description="📊 View moderation statistics")
    @is_mod()
    async def modstats(self, ctx: commands.Context, moderator: Optional[discord.Member] = None):
        await self._modstats_logic(ctx, moderator)

    # Slash command - registered dynamically in __init__.py
    async def modstats_slash(self, interaction: discord.Interaction, moderator: Optional[discord.Member] = None):
        await self._modstats_logic(interaction, moderator)
