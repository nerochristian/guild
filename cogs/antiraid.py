"""
AntiRaid - Advanced raid protection with Gemini AI pattern detection

Features:
- Automatic raid detection based on join velocity
- AI-powered analysis of member patterns (names, account age, avatars)
- Multiple response actions (kick, ban, lockdown, quarantine)
- Manual raid mode toggle
- Whitelist system for verified users
- Detailed logging and analytics
"""

import os
import re
import json
import asyncio
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal, Dict, Any, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiohttp

DO_API_KEY = os.getenv("DO_API_KEY", "")
DO_BASE_URL = "https://inference.do-ai.run/v1"

from utils.embeds import ModEmbed
from utils.logging import send_log_embed
from utils.checks import is_admin, is_mod, is_bot_owner_id
from config import Config


# =========================
# AI Raid Analysis Helper
# =========================

class AIRaidAnalyzer:
    """
    Uses Gemini to detect sophisticated raid patterns by analyzing:
    - Username similarity and bot-like patterns
    - Account ages (new accounts are suspicious)
    - Avatar/banner presence (default avatars in bulk)
    - Join timing patterns
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = DO_API_KEY
        self.client = True # Dummy flag for compat

        self.analysis_cache: Dict[str, tuple[dict, datetime]] = {}
        self.rate_limit_tracker: Dict[str, List[datetime]] = defaultdict(list)

        # config
        self.cache_ttl_seconds = 180  # 3 min cache for raid analysis
        self.max_requests_per_minute = 20  # conservative for raid checks
        self.model = "gemini-2.5-flash"  # fast model for real-time

    def _hash_members(self, members: List[discord.Member]) -> str:
        """Create cache key from member data"""
        key_data = ",".join(sorted([f"{m.id}:{m.name}" for m in members]))
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()

    def _within_rate_limit(self) -> bool:
        now = datetime.now(timezone.utc)
        window = timedelta(seconds=60)
        events = [
            t for t in self.rate_limit_tracker["requests"]
            if now - t < window
        ]
        self.rate_limit_tracker["requests"] = events
        return len(events) < self.max_requests_per_minute

    def _record_request(self) -> None:
        self.rate_limit_tracker["requests"].append(datetime.now(timezone.utc))

    def _get_cached(self, members: List[discord.Member]) -> Optional[dict]:
        h = self._hash_members(members)
        if h not in self.analysis_cache:
            return None
        data, ts = self.analysis_cache[h]
        if (datetime.now(timezone.utc) - ts).total_seconds() > self.cache_ttl_seconds:
            del self.analysis_cache[h]
            return None
        return data

    def _set_cache(self, members: List[discord.Member], result: dict) -> None:
        h = self._hash_members(members)
        self.analysis_cache[h] = (result, datetime.now(timezone.utc))

    async def analyze_raid_pattern(
        self,
        guild: discord.Guild,
        recent_members: List[discord.Member],
    ) -> dict:
        """
        Analyze member join patterns to detect raids.
        
        Returns:
        {
            "is_raid": bool,
            "confidence": 0-100,
            "pattern": str description,
            "recommended_action": "kick|ban|lockdown|quarantine",
            "severity": 1-10
        }
        """
        if not self.client or not recent_members:
            return {
                "is_raid": False,
                "confidence": 0,
                "pattern": "no_data",
                "recommended_action": "kick",
                "severity": 1,
            }

        # check cache
        cached = self._get_cached(recent_members)
        if cached is not None:
            return cached

        # rate limit check
        if not self._within_rate_limit():
            return {
                "is_raid": False,
                "confidence": 0,
                "pattern": "rate_limited",
                "recommended_action": "kick",
                "severity": 1,
            }

        # extract member data (sample last 25 to keep prompt manageable)
        now = datetime.now(timezone.utc)
        member_data = []
        for member in recent_members[-25:]:
            account_age_days = (now - member.created_at.replace(tzinfo=timezone.utc)).days
            join_age_seconds = (
                (now - member.joined_at.replace(tzinfo=timezone.utc)).total_seconds()
                if member.joined_at
                else 0
            )
            
            member_data.append({
                "name": member.name,
                "discriminator": member.discriminator,
                "account_age_days": account_age_days,
                "has_avatar": member.avatar is not None,
                "has_banner": member.banner is not None,
                "join_seconds_ago": int(join_age_seconds),
                "is_bot": member.bot,
            })

        prompt = f"""
You are a Discord raid detection AI.

Guild: {guild.name} ({guild.member_count} members)
Recent joins (last {len(member_data)} members):
{json.dumps(member_data, indent=2)}

Analyze these joins for raid patterns. Look for:
1. Similar/sequential usernames (e.g., user1, user2, user3 or raid123, raid456)
2. Many new accounts (< 7 days old)
3. Bulk default avatars (has_avatar: false)
4. Very rapid joins (join_seconds_ago all < 30)
5. Bot accounts joining in bulk

Respond ONLY with valid JSON:
{{
  "is_raid": bool,
  "confidence": 0-100,
  "pattern": "brief description of detected pattern",
  "recommended_action": "kick|ban|lockdown|quarantine",
  "severity": 1-10
}}

Guidelines:
- confidence 80+ = definite raid
- confidence 60-79 = probable raid
- confidence < 60 = not a raid
- severity 8+ = immediate lockdown
- severity 5-7 = kick/ban
- severity < 5 = quarantine/watch
"""

        try:
            payload = {
                "model": "deepseek-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.15,
                "max_tokens": 250,
                "response_format": {"type": "json_object"}
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{DO_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"API Error {resp.status}")
                    data = await resp.json()
            
            self._record_request()
            raw = data["choices"][0]["message"]["content"].strip()

            # parse json
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                return {
                    "is_raid": False,
                    "confidence": 0,
                    "pattern": "parse_error",
                    "recommended_action": "kick",
                    "severity": 1,
                }

            data = json.loads(raw[start : end + 1])

            result = {
                "is_raid": bool(data.get("is_raid", False)),
                "confidence": max(0, min(100, int(data.get("confidence", 0)))),
                "pattern": str(data.get("pattern", "unknown"))[:200],
                "recommended_action": str(
                    data.get("recommended_action", "kick")
                ).lower(),
                "severity": max(1, min(10, int(data.get("severity", 1)))),
            }

            # validate action
            if result["recommended_action"] not in [
                "kick",
                "ban",
                "lockdown",
                "quarantine",
            ]:
                result["recommended_action"] = "kick"

            self._set_cache(recent_members, result)
            return result

        except Exception as e:
            print(f"[Gemini AntiRaid] Error analyzing raid: {e}")
            return {
                "is_raid": False,
                "confidence": 0,
                "pattern": "exception",
                "recommended_action": "kick",
                "severity": 1,
            }


# =========================
# AntiRaid Cog
# =========================

class AntiRaid(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.join_tracker: Dict[int, List[datetime]] = defaultdict(list)
        self.member_tracker: Dict[int, List[discord.Member]] = defaultdict(list)
        self.raid_cooldown: set[int] = set()  # guilds on cooldown after raid response
        self.ai_analyzer = AIRaidAnalyzer()
        
        # start background tasks
        self.check_raids.start()
        self.cleanup_trackers.start()

    def cog_unload(self):
        self.check_raids.cancel()
        self.cleanup_trackers.cancel()

    # ---------- background tasks ----------

    @tasks.loop(seconds=5)
    async def check_raids(self):
        """Periodic check for raid patterns"""
        now = datetime.now(timezone.utc)
        for guild_id, joins in list(self.join_tracker.items()):
            # clean old entries (keep last 60 seconds)
            self.join_tracker[guild_id] = [
                t for t in joins if (now - t).total_seconds() < 60
            ]
            if not self.join_tracker[guild_id]:
                del self.join_tracker[guild_id]

    @tasks.loop(minutes=5)
    async def cleanup_trackers(self):
        """Clean up old member references"""
        now = datetime.now(timezone.utc)
        for guild_id, members in list(self.member_tracker.items()):
            self.member_tracker[guild_id] = [
                m for m in members
                if m.joined_at
                and (now - m.joined_at.replace(tzinfo=timezone.utc)).total_seconds() < 300
            ]
            if not self.member_tracker[guild_id]:
                del self.member_tracker[guild_id]

    # ---------- raid detection logic ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Detect and respond to potential raids"""
        if member.bot:
            return  # ignore bots for now (can be configured later)

        if is_bot_owner_id(member.id):
            return

        settings = await self.bot.db.get_settings(member.guild.id)
        if not settings.get("antiraid_enabled", False):
            return

        # check if guild is on cooldown
        if member.guild.id in self.raid_cooldown:
            # still in raid mode, handle new joiner
            await self._handle_raid_mode_join(member, settings)
            return

        # track join
        now = datetime.now(timezone.utc)
        self.join_tracker[member.guild.id].append(now)
        self.member_tracker[member.guild.id].append(member)

        # check basic threshold
        threshold = settings.get("antiraid_join_threshold", 10)
        seconds = settings.get("antiraid_join_seconds", 10)

        # count recent joins
        cutoff = now - timedelta(seconds=seconds)
        recent_joins = [
            t for t in self.join_tracker[member.guild.id]
            if t >= cutoff
        ]

        if len(recent_joins) >= threshold:
            # potential raid detected
            await self._process_potential_raid(member.guild, settings)

    async def _handle_raid_mode_join(
        self,
        member: discord.Member,
        settings: dict,
    ):
        """Handle new joins during active raid mode"""
        if is_bot_owner_id(member.id):
            return

        action = settings.get("antiraid_raidmode_action", "kick")
        
        try:
            if action == "kick":
                await member.kick(reason="[ANTI-RAID] Server in raid mode")
            elif action == "ban":
                await member.ban(
                    reason="[ANTI-RAID] Server in raid mode",
                    delete_message_days=0,
                )
            elif action == "quarantine":
                # assign quarantine role if configured
                quarantine_role_id = settings.get("antiraid_quarantine_role")
                if quarantine_role_id:
                    role = member.guild.get_role(quarantine_role_id)
                    if role:
                        await member.add_roles(
                            role,
                            reason="[ANTI-RAID] Quarantine during raid mode",
                        )
        except Exception as e:
            print(f"[AntiRaid] Failed to handle raid mode join: {e}")

    async def _process_potential_raid(
        self,
        guild: discord.Guild,
        settings: dict,
    ):
        """Process a potential raid with AI analysis if enabled"""
        # check if AI is enabled
        if settings.get("antiraid_ai_enabled", False):
            recent_members = self.member_tracker[guild.id][-30:]  # last 30 joins
            
            if len(recent_members) >= 5:  # need at least 5 for pattern analysis
                analysis = await self.ai_analyzer.analyze_raid_pattern(
                    guild,
                    recent_members,
                )

                min_confidence = settings.get("antiraid_ai_min_confidence", 70)
                
                if analysis["is_raid"] and analysis["confidence"] >= min_confidence:
                    # AI detected raid
                    action = analysis.get("recommended_action", "kick")
                    
                    # respect server override if configured
                    if settings.get("antiraid_override_ai_action", False):
                        action = settings.get("antiraid_action", "kick")
                    
                    await self.trigger_raid_response(
                        guild,
                        settings,
                        action=action,
                        ai_analysis=analysis,
                    )
                    return

        # fallback to standard raid response
        await self.trigger_raid_response(guild, settings)

    async def trigger_raid_response(
        self,
        guild: discord.Guild,
        settings: dict,
        action: Optional[str] = None,
        ai_analysis: Optional[dict] = None,
    ):
        """Execute raid response actions"""
        if guild.id in self.raid_cooldown:
            return  # already responded

        # add to cooldown
        self.raid_cooldown.add(guild.id)

        # determine action
        if action is None:
            action = settings.get("antiraid_action", "kick")

        # build log embed
        embed = discord.Embed(
            title="🚨 RAID DETECTED",
            description=f"Automatic raid protection triggered!\n**Action:** {action.upper()}",
            color=Config.COLOR_ERROR,
            timestamp=datetime.now(timezone.utc),
        )

        if ai_analysis:
            embed.add_field(
                name="🤖 AI Analysis",
                value=(
                    f"**Confidence:** {ai_analysis['confidence']}%\n"
                    f"**Severity:** {ai_analysis['severity']}/10\n"
                    f"**Pattern:** {ai_analysis['pattern']}"
                ),
                inline=False,
            )

        # log the detection
        log_channel_id = settings.get("mod_log_channel")
        if log_channel_id:
            channel = guild.get_channel(log_channel_id)
            if channel:
                try:
                    await send_log_embed(channel, embed)
                except Exception:
                    pass

        # execute action
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=30)
        action_count = 0

        if action == "lockdown":
            # lock all text channels
            for channel in guild.text_channels:
                try:
                    await channel.set_permissions(
                        guild.default_role,
                        send_messages=False,
                        reason="[ANTI-RAID] Automatic lockdown",
                    )
                    action_count += 1
                except Exception:
                    pass

            # enable raid mode
            settings["raid_mode"] = True
            await self.bot.db.update_settings(guild.id, settings)

        elif action in ["kick", "ban"]:
            # get recent joiners from tracker
            recent_members = [
                m for m in self.member_tracker[guild.id]
                if m.joined_at
                and m.joined_at.replace(tzinfo=timezone.utc) >= cutoff
            ]

            for member in recent_members:
                if is_bot_owner_id(member.id):
                    continue
                try:
                    if action == "kick":
                        await member.kick(reason="[ANTI-RAID] Automatic raid protection")
                    else:
                        await member.ban(
                            reason="[ANTI-RAID] Automatic raid protection",
                            delete_message_days=1,
                        )
                    action_count += 1
                except Exception:
                    pass

        elif action == "quarantine":
            # add quarantine role to recent joiners
            quarantine_role_id = settings.get("antiraid_quarantine_role")
            if quarantine_role_id:
                role = guild.get_role(quarantine_role_id)
                if role:
                    recent_members = [
                        m for m in self.member_tracker[guild.id]
                        if m.joined_at
                        and m.joined_at.replace(tzinfo=timezone.utc) >= cutoff
                    ]
                    for member in recent_members:
                        if is_bot_owner_id(member.id):
                            continue
                        try:
                            await member.add_roles(
                                role,
                                reason="[ANTI-RAID] Quarantine",
                            )
                            action_count += 1
                        except Exception:
                            pass

        # update log with action count
        if action_count > 0:
            embed.add_field(
                name="📊 Actions Taken",
                value=f"**{action_count}** {action}s executed",
                inline=False,
            )
            if log_channel_id:
                channel = guild.get_channel(log_channel_id)
                if channel:
                    try:
                        await send_log_embed(channel, embed)
                    except Exception:
                        pass

        # clear trackers
        self.join_tracker[guild.id] = []
        self.member_tracker[guild.id] = []

        # remove from cooldown after delay
        async def _remove_cooldown():
            await asyncio.sleep(settings.get("antiraid_cooldown_seconds", 60))
            self.raid_cooldown.discard(guild.id)

        asyncio.create_task(_remove_cooldown())

    # =========================
    # SLASH COMMANDS
    # =========================

    antiraid_group = app_commands.Group(
        name="antiraid",
        description="Anti-raid protection configuration",
    )

    # --- enable/disable ---

    @antiraid_group.command(name="enable", description="Enable anti-raid protection")
    @is_admin()
    async def antiraid_enable(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["antiraid_enabled"] = True
        await self.bot.db.update_settings(interaction.guild_id, settings)
        embed = ModEmbed.success(
            "Anti-Raid Enabled",
            "Anti-raid protection is now active.",
        )
        await interaction.response.send_message(embed=embed)

    @antiraid_group.command(name="disable", description="Disable anti-raid protection")
    @is_admin()
    async def antiraid_disable(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["antiraid_enabled"] = False
        await self.bot.db.update_settings(interaction.guild_id, settings)
        embed = ModEmbed.success(
            "Anti-Raid Disabled",
            "Anti-raid protection has been disabled.",
        )
        await interaction.response.send_message(embed=embed)

    # --- settings ---

    @antiraid_group.command(name="settings", description="Configure anti-raid detection")
    @app_commands.describe(
        threshold="Number of joins to trigger detection (default: 10)",
        seconds="Time window in seconds (default: 10)",
        cooldown="Cooldown between raid responses in seconds (default: 60)",
    )
    @is_admin()
    async def antiraid_settings(
        self,
        interaction: discord.Interaction,
        threshold: Optional[int] = None,
        seconds: Optional[int] = None,
        cooldown: Optional[int] = None,
    ):
        settings = await self.bot.db.get_settings(interaction.guild_id)

        if threshold is not None:
            settings["antiraid_join_threshold"] = max(3, threshold)

        if seconds is not None:
            settings["antiraid_join_seconds"] = max(5, seconds)

        if cooldown is not None:
            settings["antiraid_cooldown_seconds"] = max(10, cooldown)

        await self.bot.db.update_settings(interaction.guild_id, settings)

        embed = discord.Embed(
            title="🛡️ Anti-Raid Settings",
            color=Config.COLOR_INFO,
        )
        embed.add_field(
            name="Threshold",
            value=f"{settings.get('antiraid_join_threshold', 10)} joins",
            inline=True,
        )
        embed.add_field(
            name="Time Window",
            value=f"{settings.get('antiraid_join_seconds', 10)} seconds",
            inline=True,
        )
        embed.add_field(
            name="Cooldown",
            value=f"{settings.get('antiraid_cooldown_seconds', 60)} seconds",
            inline=True,
        )
        embed.add_field(
            name="Action",
            value=settings.get("antiraid_action", "kick").title(),
            inline=True,
        )
        embed.add_field(
            name="AI Detection",
            value="✅ Enabled" if settings.get("antiraid_ai_enabled") else "❌ Disabled",
            inline=True,
        )

        await interaction.response.send_message(embed=embed)

    @antiraid_group.command(name="action", description="Set raid response action")
    @app_commands.describe(
        action="Action to take when raid is detected",
    )
    @is_admin()
    async def antiraid_action(
        self,
        interaction: discord.Interaction,
        action: Literal["kick", "ban", "lockdown", "quarantine"],
    ):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["antiraid_action"] = action
        await self.bot.db.update_settings(interaction.guild_id, settings)
        embed = ModEmbed.success(
            "Action Updated",
            f"Anti-raid action set to **{action}**",
        )
        await interaction.response.send_message(embed=embed)

    # --- quarantine role ---

    @antiraid_group.command(
        name="quarantine",
        description="Set quarantine role for raid suspects",
    )
    @app_commands.describe(
        role="Role to assign to suspected raiders (leave empty to disable)",
    )
    @is_admin()
    async def antiraid_quarantine(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
    ):
        settings = await self.bot.db.get_settings(interaction.guild_id)

        if role is None:
            settings["antiraid_quarantine_role"] = None
            await self.bot.db.update_settings(interaction.guild_id, settings)
            embed = ModEmbed.success(
                "Quarantine Disabled",
                "Quarantine role has been removed.",
            )
        else:
            settings["antiraid_quarantine_role"] = role.id
            await self.bot.db.update_settings(interaction.guild_id, settings)
            embed = ModEmbed.success(
                "Quarantine Role Set",
                f"Quarantine role set to {role.mention}",
            )

        await interaction.response.send_message(embed=embed)

    # --- AI config ---

    @antiraid_group.command(
        name="ai",
        description="Configure AI-powered raid detection",
    )
    @app_commands.describe(
        enabled="Enable or disable AI analysis",
        min_confidence="Minimum confidence (0-100) to trigger (default: 70)",
        override_action="Let AI choose action vs use configured action",
    )
    @is_admin()
    async def antiraid_ai(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        min_confidence: Optional[int] = None,
        override_action: Optional[bool] = None,
    ):
        settings = await self.bot.db.get_settings(interaction.guild_id)

        if enabled is not None:
            settings["antiraid_ai_enabled"] = enabled

        if min_confidence is not None:
            settings["antiraid_ai_min_confidence"] = max(0, min(100, min_confidence))

        if override_action is not None:
            settings["antiraid_override_ai_action"] = override_action

        await self.bot.db.update_settings(interaction.guild_id, settings)

        ai_status = (
            "enabled" if settings.get("antiraid_ai_enabled", False) else "disabled"
        )
        conf = settings.get("antiraid_ai_min_confidence", 70)
        override = settings.get("antiraid_override_ai_action", False)

        embed = ModEmbed.success(
            "AI Detection Updated",
            (
                f"Gemini AI detection is **{ai_status}**.\n"
                f"Min confidence: **{conf}%**\n"
                f"Override AI action: **{'Yes' if override else 'No'}**"
            ),
        )
        await interaction.response.send_message(embed=embed)

    # --- manual raid mode ---

    @antiraid_group.command(name="raidmode", description="Manually toggle raid mode")
    @app_commands.describe(
        enabled="Enable or disable raid mode",
    )
    @is_admin()
    async def raidmode(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["raid_mode"] = enabled
        await self.bot.db.update_settings(interaction.guild_id, settings)

        await interaction.response.defer()

        if enabled:
            # lock all text channels
            locked_count = 0
            for channel in interaction.guild.text_channels:
                try:
                    await channel.set_permissions(
                        interaction.guild.default_role,
                        send_messages=False,
                        reason=f"[RAID MODE] Enabled by {interaction.user}",
                    )
                    locked_count += 1
                except Exception:
                    pass

            embed = discord.Embed(
                title="🚨 RAID MODE ENABLED",
                description=(
                    f"Locked **{locked_count}** channels.\n"
                    "New members will be auto-kicked/quarantined."
                ),
                color=Config.COLOR_ERROR,
            )
        else:
            # unlock all text channels
            unlocked_count = 0
            for channel in interaction.guild.text_channels:
                try:
                    await channel.set_permissions(
                        interaction.guild.default_role,
                        send_messages=None,
                        reason=f"[RAID MODE] Disabled by {interaction.user}",
                    )
                    unlocked_count += 1
                except Exception:
                    pass

            embed = discord.Embed(
                title="✅ Raid Mode Disabled",
                description=f"Unlocked **{unlocked_count}** channels.",
                color=Config.COLOR_SUCCESS,
            )

        await interaction.followup.send(embed=embed)

    # --- stats ---

    @antiraid_group.command(name="stats", description="View anti-raid statistics")
    @is_mod()
    async def antiraid_stats(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        
        embed = discord.Embed(
            title="📊 Anti-Raid Statistics",
            color=Config.COLOR_INFO,
            timestamp=datetime.now(timezone.utc),
        )

        # current status
        raid_mode = settings.get("raid_mode", False)
        enabled = settings.get("antiraid_enabled", False)
        
        status = "🟢 Active" if enabled else "🔴 Disabled"
        if raid_mode:
            status += " | 🚨 **RAID MODE**"
        
        embed.add_field(
            name="Status",
            value=status,
            inline=False,
        )

        # current tracking
        guild_id = interaction.guild_id
        recent_joins = len(self.join_tracker.get(guild_id, []))
        tracked_members = len(self.member_tracker.get(guild_id, []))
        
        embed.add_field(
            name="Recent Activity",
            value=(
                f"**Joins (last 60s):** {recent_joins}\n"
                f"**Tracked Members:** {tracked_members}"
            ),
            inline=True,
        )

        # config
        threshold = settings.get("antiraid_join_threshold", 10)
        seconds = settings.get("antiraid_join_seconds", 10)
        action = settings.get("antiraid_action", "kick")
        
        embed.add_field(
            name="Configuration",
            value=(
                f"**Threshold:** {threshold} joins / {seconds}s\n"
                f"**Action:** {action.title()}"
            ),
            inline=True,
        )

        # ai status
        ai_enabled = settings.get("antiraid_ai_enabled", False)
        if ai_enabled:
            confidence = settings.get("antiraid_ai_min_confidence", 70)
            embed.add_field(
                name="🤖 AI Detection",
                value=f"Enabled (min {confidence}% confidence)",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(AntiRaid(bot))
