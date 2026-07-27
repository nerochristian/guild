"""
Voice Moderation Commands
"""

import discord
import asyncio
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from typing import Optional, Literal, Union
from utils.embeds import ModEmbed
from utils.checks import is_mod, is_admin, is_bot_owner_id, can_moderate
from config import Config

# TTS for voice announcements
try:
    from utils.tts import speak_in_vc, Announcements, get_tts_engine
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    def get_tts_engine(): return "None"

# Voice options: "male" or "female" - can be changed per server in future
TTS_VOICE_GENDER = "female"  # Options: "male" or "female"
TTS_VOICES = {
    "male": "adam",      # ElevenLabs: deep authoritative / Edge: guy
    "female": "rachel",  # ElevenLabs: professional female / Edge: aria
}


class PresenceCheckView(discord.ui.View):
    """View for presence check DMs."""
    
    def __init__(self, user_id: int, responded_set: set):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.responded_set = responded_set
    
    @discord.ui.button(label="I'm Here!", style=discord.ButtonStyle.success, emoji="👋")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't for you!", ephemeral=True)
        
        self.responded_set.add(self.user_id)
        button.disabled = True
        button.label = "Confirmed!"
        
        await interaction.response.edit_message(view=self)
        self.stop()


class VoiceSelectView(discord.ui.View):
    """Interactive voice selection with preview and accept/deny."""
    
    SAMPLE_TEXT = "Hello! This is a sample of my voice. How do I sound?"
    
    VOICES = {
        "adam": ("👨 Adam", "Deep, authoritative"),
        "antoni": ("👨 Antoni", "Warm, friendly"),
        "josh": ("👨 Josh", "Young professional"),
        "rachel": ("👩 Rachel", "Professional"),
        "bella": ("👩 Bella", "Soft, warm"),
        "domi": ("👩 Domi", "Strong, confident"),
    }
    
    def __init__(self, user_id: int, bot, current_voice: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.bot = bot
        self.current_voice = current_voice
        self.selected_voice = current_voice
        self.previewing = False
        
        # Add voice selection dropdown
        self.voice_select = discord.ui.Select(
            placeholder=f"Select a voice (current: {current_voice})",
            options=[
                discord.SelectOption(label=name, value=voice_id, description=desc, default=(voice_id == current_voice))
                for voice_id, (name, desc) in self.VOICES.items()
            ]
        )
        self.voice_select.callback = self.on_voice_select
        self.add_item(self.voice_select)
    
    async def on_voice_select(self, interaction: discord.Interaction):
        """Handle voice selection - play preview."""
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't for you!", ephemeral=True)
        
        self.selected_voice = self.voice_select.values[0]
        voice_name, voice_desc = self.VOICES[self.selected_voice]
        
        # Check if user is in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=ModEmbed.warning("Join a Voice Channel", "Join a voice channel first to hear the preview!"),
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Connect to voice channel
        channel = interaction.user.voice.channel
        voice_client = None
        try:
            voice_client = interaction.guild.voice_client
            if voice_client:
                await voice_client.move_to(channel)
            else:
                voice_client = await channel.connect(timeout=10.0)
        except Exception as e:
            await interaction.followup.send(
                embed=ModEmbed.error("Connection Failed", f"Couldn't join your voice channel: {e}"),
                ephemeral=True
            )
            return
        
        # Play preview
        try:
            from utils.tts import speak_in_vc
            await speak_in_vc(voice_client, self.SAMPLE_TEXT, voice=self.selected_voice)
            
            # Show confirmation view
            confirm_embed = discord.Embed(
                title=f"🔊 Preview: {voice_name}",
                description=f"*\"{self.SAMPLE_TEXT}\"*\n\n{voice_desc}\n\n**Do you want to use this voice?**",
                color=Config.COLOR_INFO,
            )
            
            # Disconnect after preview
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
            
            await interaction.followup.send(embed=confirm_embed, view=VoiceConfirmView(self.user_id, self.bot, self.selected_voice), ephemeral=True)
            
        except Exception as e:
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
            await interaction.followup.send(
                embed=ModEmbed.error("Preview Failed", f"Couldn't play preview: {e}"),
                ephemeral=True
            )


class VoiceConfirmView(discord.ui.View):
    """Accept or deny the selected voice."""
    
    def __init__(self, user_id: int, bot, voice: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bot = bot
        self.voice = voice
    
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't for you!", ephemeral=True)
        
        # Save to database
        await self.bot.db.update_settings(interaction.guild_id, {"tts_voice": self.voice})
        
        # Update embed
        embed = discord.Embed(
            title="✅ Voice Saved!",
            description=f"TTS voice has been set to **{self.voice}**.\n\nThis voice will be used for all presence check announcements.",
            color=Config.COLOR_SUCCESS,
        )
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()
    
    @discord.ui.button(label="Try Another", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't for you!", ephemeral=True)
        
        embed = discord.Embed(
            title="🔄 Cancelled",
            description="Voice not changed. Use `/vc voice` to try again.",
            color=Config.COLOR_INFO,
        )
        
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()



class Voice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    # Create command group
    vc_group = app_commands.Group(name="vc", description="🎤 Voice channel moderation commands")
    
    # ─────────────────────────────────────────────────────────────────────────
    # Basic Voice Moderation Subcommands
    # ─────────────────────────────────────────────────────────────────────────
    
    @vc_group.command(name="mute", description="Server mute a user in voice")
    @app_commands.describe(user="The user to mute", reason="Reason for muting")
    @is_mod()
    async def vc_mute(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):

        await self._mute(interaction, user, reason)
    
    @vc_group.command(name="unmute", description="Server unmute a user in voice")
    @app_commands.describe(user="The user to unmute")
    @is_mod()
    async def vc_unmute(self, interaction: discord.Interaction, user: discord.Member):
        await self._unmute(interaction, user)
    
    @vc_group.command(name="deafen", description="Server deafen a user in voice")
    @app_commands.describe(user="The user to deafen", reason="Reason for deafening")
    @is_mod()
    async def vc_deafen(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):

        await self._deafen(interaction, user, reason)
    
    @vc_group.command(name="undeafen", description="Server undeafen a user in voice")
    @app_commands.describe(user="The user to undeafen")
    @is_mod()
    async def vc_undeafen(self, interaction: discord.Interaction, user: discord.Member):
        await self._undeafen(interaction, user)
    
    @vc_group.command(name="kick", description="Disconnect a user from voice channel")
    @app_commands.describe(user="The user to kick from VC", reason="Reason for kicking")
    @is_mod()
    async def vc_kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):

        await self._kick(interaction, user, reason)
    
    @vc_group.command(name="move", description="Move a user to another voice channel")
    @app_commands.describe(user="The user to move", channel="Target voice channel")
    @is_mod()
    async def vc_move(self, interaction: discord.Interaction, user: discord.Member, channel: discord.VoiceChannel):
        await self._move(interaction, user, channel)
    
    @vc_group.command(name="moveall", description="Move all users from one channel to another")
    @app_commands.describe(from_channel="Source voice channel", to_channel="Target voice channel")
    @is_mod()
    async def vc_moveall(self, interaction: discord.Interaction, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel):
        await self._moveall(interaction, from_channel, to_channel)

    @vc_group.command(name="muteall", description="Server mute all users in a voice channel")
    @app_commands.describe(channel="Voice channel to mute", reason="Reason for muting everyone")
    @is_mod()
    async def vc_muteall(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        reason: str = "No reason provided",
    ):
        await self._muteall(interaction, channel, reason)

    @vc_group.command(name="unmuteall", description="Server unmute all users in a voice channel")
    @app_commands.describe(channel="Voice channel to unmute", reason="Reason for unmuting everyone")
    @is_mod()
    async def vc_unmuteall(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        reason: str = "No reason provided",
    ):
        await self._unmuteall(interaction, channel, reason)
    
    @vc_group.command(name="ban", description="Ban a user from all voice channels")
    @app_commands.describe(user="The user to voice ban", reason="Reason for banning")
    @is_mod()
    async def vc_ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):

        await self._ban(interaction, user, reason)
    
    @vc_group.command(name="unban", description="Unban a user from voice channels")
    @app_commands.describe(user="The user to unban", user_id="User ID if they left the server")
    @is_mod()
    async def vc_unban(self, interaction: discord.Interaction, user: Optional[discord.Member] = None, user_id: Optional[str] = None):
        target_id = None
        if user:
            target_id = user.id
        elif user_id:
            try:
                target_id = int(user_id)
            except ValueError:
                return await interaction.response.send_message(embed=ModEmbed.error("Invalid ID", "Please provide a valid user ID."), ephemeral=True)
        else:
            return await interaction.response.send_message(embed=ModEmbed.error("Missing Argument", "Please specify `user` or `user_id`."), ephemeral=True)
        await self._unban(interaction, target_id)
    
    # ─────────────────────────────────────────────────────────────────────────
    # Presence Check Subcommands
    # ─────────────────────────────────────────────────────────────────────────
    
    @vc_group.command(name="check", description="Presence check - verify users are active in voice")
    @app_commands.describe(channel="Check all users in this channel", user="Check a specific user")
    @is_mod()
    async def vc_check(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None, user: Optional[discord.Member] = None):
        verification_cog = self.bot.get_cog("Verification")
        if not verification_cog:
            return await interaction.response.send_message(embed=ModEmbed.error("Not Available", "Verification cog is not loaded."), ephemeral=True)
        
        if channel:
            await self._manual_verify_channel(interaction, verification_cog, channel)
        elif user:
            await self._manual_verify_user(interaction, verification_cog, user)
        else:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Missing Argument", "Specify `channel` to check all users or `user` to check one person."),
                ephemeral=True
            )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Verification Settings Subcommands (Admin Only)
    # ─────────────────────────────────────────────────────────────────────────
    
    @vc_group.command(name="verify", description="Toggle voice verification requirement")
    @app_commands.describe(state="Enable or disable voice verification")
    @is_admin()
    async def vc_verify(self, interaction: discord.Interaction, state: Literal["on", "off"]):
        await self._verification(interaction, state)
    
    @vc_group.command(name="verify-settings", description="View voice verification settings")
    @is_admin()
    async def vc_verify_settings(self, interaction: discord.Interaction):
        verification_cog = self.bot.get_cog("Verification")
        if not verification_cog:
            return await interaction.response.send_message(embed=ModEmbed.error("Not Available", "Verification cog is not loaded."), ephemeral=True)
        await verification_cog.show_settings(interaction)
    
    @vc_group.command(name="verify-bypass", description="Add or remove a bypass role for verification")
    @app_commands.describe(action="Add or remove the role", role="The role to add/remove from bypass list")
    @is_admin()
    async def vc_verify_bypass(self, interaction: discord.Interaction, action: Literal["add", "remove"], role: discord.Role):
        verification_cog = self.bot.get_cog("Verification")
        if not verification_cog:
            return await interaction.response.send_message(embed=ModEmbed.error("Not Available", "Verification cog is not loaded."), ephemeral=True)
        
        if action == "add":
            await verification_cog.add_bypass_role(interaction, role)
        else:
            await verification_cog.remove_bypass_role(interaction, role)
    
    @vc_group.command(name="verify-timeout", description="Set verification session timeout")
    @app_commands.describe(minutes="Session timeout in minutes (1-1440)")
    @is_admin()
    async def vc_verify_timeout(self, interaction: discord.Interaction, minutes: int):
        verification_cog = self.bot.get_cog("Verification")
        if not verification_cog:
            return await interaction.response.send_message(embed=ModEmbed.error("Not Available", "Verification cog is not loaded."), ephemeral=True)
        await verification_cog.set_session_timeout(interaction, minutes)
    
    # ─────────────────────────────────────────────────────────────────────────
    # TTS Voice Settings
    # ─────────────────────────────────────────────────────────────────────────
    
    @vc_group.command(name="voice", description="🔊 Configure TTS voice for announcements")
    @is_admin()
    async def vc_voice(self, interaction: discord.Interaction):
        """Interactive voice selection with previews."""
        if not TTS_AVAILABLE:
            return await interaction.response.send_message(
                embed=ModEmbed.error("TTS Unavailable", "Text-to-speech is not configured."),
                ephemeral=True
            )
        
        # Get current settings
        settings = await self.bot.db.get_settings(interaction.guild_id)
        current_voice = settings.get("tts_voice", "rachel")
        
        # Create voice selection embed
        embed = discord.Embed(
            title="🔊 TTS Voice Settings",
            description=(
                f"**Current Voice:** `{current_voice}`\n"
                f"**TTS Engine:** `{get_tts_engine()}`\n\n"
                "Select a voice below to preview it, then click **Accept** to save."
            ),
            color=Config.COLOR_INFO,
        )
        
        # Add voice options
        embed.add_field(
            name="👨 Male Voices",
            value="• **Adam** - Deep, authoritative\n• **Antoni** - Warm, friendly\n• **Josh** - Young professional",
            inline=True
        )
        embed.add_field(
            name="👩 Female Voices", 
            value="• **Rachel** - Professional\n• **Bella** - Soft, warm\n• **Domi** - Strong, confident",
            inline=True
        )
        
        # Create view with voice buttons
        view = VoiceSelectView(interaction.user.id, self.bot, current_voice)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _respond(self, source, embed=None, content=None, ephemeral=False, view=None):
        if isinstance(source, discord.Interaction):
            if source.response.is_done():
                return await source.followup.send(content=content, embed=embed, ephemeral=ephemeral, view=view)
            else:
                return await source.response.send_message(content=content, embed=embed, ephemeral=ephemeral, view=view)
        else:
            return await source.send(content=content, embed=embed, view=view)

    async def _manual_verify_channel(self, interaction: discord.Interaction, verification_cog, channel: discord.VoiceChannel):
        """Presence check: Bot joins VC, announces via TTS, waits for responses, kicks non-responders."""
        
        if not channel.members:
            return await interaction.response.send_message(
                embed=ModEmbed.info("Empty Channel", f"{channel.mention} has no members to check."),
                ephemeral=True
            )
        
        # Filter to non-bot members
        members_to_check = [m for m in channel.members if not m.bot]
        if not members_to_check:
            return await interaction.response.send_message(
                embed=ModEmbed.info("No Users", f"{channel.mention} only has bots."),
                ephemeral=True
            )
        
        await interaction.response.defer(ephemeral=True)
        
        # Try to join the voice channel
        voice_client = None
        try:
            voice_client = await channel.connect(timeout=10.0)
        except discord.ClientException:
            # Already connected somewhere, try to move
            if interaction.guild.voice_client:
                voice_client = interaction.guild.voice_client
                await voice_client.move_to(channel)
        except Exception:
            pass
        
        # Get saved voice from settings
        settings = await self.bot.db.get_settings(interaction.guild_id)
        tts_voice = settings.get("tts_voice", "rachel")
        
        # 🔊 TTS ANNOUNCEMENT - Presence check start
        if voice_client and TTS_AVAILABLE:
            try:
                from utils.tts import speak_in_vc, Announcements
                await speak_in_vc(
                    voice_client, 
                    Announcements.PRESENCE_CHECK_START,
                    voice=tts_voice,
                    rate="+5%"
                )
            except Exception as e:
                print(f"TTS failed: {e}")
        
        # Track responses
        responded = set()
        check_timeout = 60  # 60 seconds to respond
        
        # Send DMs to all members
        dm_sent = 0
        dm_failed = []
        
        for member in members_to_check:
            try:
                view = PresenceCheckView(member.id, responded)
                embed = discord.Embed(
                    title="🎤 Presence Check",
                    description=(
                        f"**{interaction.guild.name}** is doing a presence check in **{channel.name}**.\n\n"
                        f"Click the button below within **{check_timeout} seconds** to confirm you're there.\n\n"
                        "⚠️ **If you don't respond, you will be disconnected from the voice channel.**"
                    ),
                    color=Config.COLOR_WARNING,
                )
                embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
                embed.set_footer(text=f"Requested by {interaction.user}")
                await member.send(embed=embed, view=view)
                dm_sent += 1
            except Exception:
                dm_failed.append(member)
        
        # Send initial status
        status_embed = discord.Embed(
            title="🔍 Presence Check Started",
            description=f"Checking **{len(members_to_check)}** members in {channel.mention}",
            color=Config.COLOR_INFO,
        )
        status_embed.add_field(name="📨 DMs Sent", value=str(dm_sent), inline=True)
        status_embed.add_field(name="⏱️ Timeout", value=f"{check_timeout}s", inline=True)
        status_embed.add_field(name="🔊 TTS", value="✅ Announced" if (voice_client and TTS_AVAILABLE) else "❌ Unavailable", inline=True)
        if dm_failed:
            status_embed.add_field(
                name="❌ DM Failed", 
                value=", ".join(m.display_name for m in dm_failed[:5]) + ("..." if len(dm_failed) > 5 else ""),
                inline=False
            )
        await interaction.followup.send(embed=status_embed, ephemeral=True)
        
        # Wait for responses
        await asyncio.sleep(check_timeout)
        
        # Process results
        kicked = 0
        present = 0
        failed_kick = 0
        
        for member in members_to_check:
            if member.id in responded:
                present += 1
                # Send thank you message
                try:
                    await member.send(
                        embed=ModEmbed.success("Thanks!", f"Thanks for confirming your presence in **{channel.name}**! 👋")
                    )
                except Exception:
                    pass
            else:
                # Kick from VC if still connected
                if member.voice and member.voice.channel:
                    try:
                        await member.move_to(None, reason=f"Presence check: No response within {check_timeout}s")
                        kicked += 1
                    except Exception:
                        failed_kick += 1
        
        # 🔊 TTS ANNOUNCEMENT - Results
        if voice_client and voice_client.is_connected() and TTS_AVAILABLE:
            try:
                result_message = Announcements.presence_kicks(kicked)
                await speak_in_vc(voice_client, result_message, voice="guy", rate="+0%")
            except Exception:
                pass
        
        # Disconnect bot from VC
        if voice_client and voice_client.is_connected():
            try:
                await voice_client.disconnect()
            except Exception:
                pass
        
        # Final report
        result_embed = discord.Embed(
            title="✅ Presence Check Complete",
            description=f"Finished checking {channel.mention}",
            color=Config.COLOR_SUCCESS,
        )
        result_embed.add_field(name="✅ Present", value=str(present), inline=True)
        result_embed.add_field(name="👢 Kicked", value=str(kicked), inline=True)
        if failed_kick > 0:
            result_embed.add_field(name="❌ Failed to Kick", value=str(failed_kick), inline=True)
        if dm_failed:
            result_embed.add_field(name="⚠️ Couldn't DM", value=str(len(dm_failed)), inline=True)
        
        await interaction.followup.send(embed=result_embed, ephemeral=True)

    async def _manual_verify_user(self, interaction: discord.Interaction, verification_cog, member: discord.Member):
        """Presence check for a single user: Bot joins, announces via TTS, waits for response."""
        
        if member.bot:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Invalid Target", "You cannot check bots."),
                ephemeral=True
            )
        
        if not member.voice or not member.voice.channel:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not In Voice", f"{member.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        channel = member.voice.channel
        await interaction.response.defer(ephemeral=True)
        
        # Try to join the voice channel
        voice_client = None
        try:
            voice_client = await channel.connect(timeout=10.0)
        except discord.ClientException:
            if interaction.guild.voice_client:
                voice_client = interaction.guild.voice_client
                await voice_client.move_to(channel)
        except Exception:
            pass
        
        # 🔊 TTS ANNOUNCEMENT - Call out the user
        if voice_client and TTS_AVAILABLE:
            try:
                await speak_in_vc(
                    voice_client, 
                    Announcements.USER_CHECK_START.format(member.display_name),
                    voice="guy",
                    rate="+0%"
                )
            except Exception:
                pass
        
        # Track response
        responded = set()
        check_timeout = 45  # 45 seconds for single user
        
        # Send DM
        try:
            view = PresenceCheckView(member.id, responded)
            embed = discord.Embed(
                title="🎤 Presence Check",
                description=(
                    f"A moderator in **{interaction.guild.name}** wants to verify you're active in **{channel.name}**.\n\n"
                    f"Click the button below within **{check_timeout} seconds** to confirm.\n\n"
                    "⚠️ **If you don't respond, you will be disconnected.**"
                ),
                color=Config.COLOR_WARNING,
            )
            embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await member.send(embed=embed, view=view)
        except Exception:
            # Disconnect and report failure
            if voice_client and voice_client.is_connected():
                try:
                    await voice_client.disconnect()
                except Exception:
                    pass
            return await interaction.followup.send(
                embed=ModEmbed.error("DM Failed", f"Could not send DM to {member.mention}. They may have DMs disabled."),
                ephemeral=True
            )
        
        # Initial status
        await interaction.followup.send(
            embed=ModEmbed.info("Checking...", f"Sent presence check to {member.mention}. Waiting {check_timeout}s for response..."),
            ephemeral=True
        )
        
        # Wait for response
        await asyncio.sleep(check_timeout)
        
        # Check result
        if member.id in responded:
            # 🔊 TTS - Thank them
            if voice_client and voice_client.is_connected() and TTS_AVAILABLE:
                try:
                    await speak_in_vc(voice_client, Announcements.USER_CHECK_THANKS, voice="guy")
                except Exception:
                    pass
            
            # Send DM thanks
            try:
                await member.send(
                    embed=ModEmbed.success("Thanks!", f"Thanks for confirming you're there! 👋")
                )
            except Exception:
                pass
            result = ModEmbed.success("Present", f"{member.mention} confirmed they're there!")
        else:
            # 🔊 TTS - Disconnecting
            if voice_client and voice_client.is_connected() and TTS_AVAILABLE:
                try:
                    await speak_in_vc(voice_client, Announcements.USER_CHECK_TIMEOUT, voice="guy")
                except Exception:
                    pass
            
            # Kick if still connected
            if member.voice and member.voice.channel:
                try:
                    await member.move_to(None, reason=f"Presence check: No response within {check_timeout}s")
                    result = ModEmbed.warning("Kicked", f"{member.mention} didn't respond and was disconnected.")
                except Exception:
                    result = ModEmbed.error("Failed", f"{member.mention} didn't respond but I couldn't kick them.")
            else:
                result = ModEmbed.info("Left", f"{member.mention} didn't respond but already left the channel.")
        
        # Disconnect bot
        if voice_client and voice_client.is_connected():
            try:
                await voice_client.disconnect()
            except Exception:
                pass
        
        await interaction.followup.send(embed=result, ephemeral=True)

    async def _mute(self, source, user: discord.Member, reason: str):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        try:
            await user.edit(mute=True, reason=f"{author}: {reason}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to mute members."), ephemeral=True)

        embed = ModEmbed.success("Voice Muted", f"{user.mention} has been server muted.\n**Reason:** {reason}")
        await self._respond(source, embed=embed)
    
    async def _unmute(self, source, user: discord.Member):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        try:
            await user.edit(mute=False, reason=f"Unmuted by {author}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to mute members."), ephemeral=True)

        embed = ModEmbed.success("Voice Unmuted", f"{user.mention} has been server unmuted.")
        await self._respond(source, embed=embed)
    
    async def _deafen(self, source, user: discord.Member, reason: str):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
            
        try:
            await user.edit(deafen=True, reason=f"{author}: {reason}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to deafen members."), ephemeral=True)

        embed = ModEmbed.success("Voice Deafened", f"{user.mention} has been server deafened.\n**Reason:** {reason}")
        await self._respond(source, embed=embed)
    
    async def _undeafen(self, source, user: discord.Member):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        try:
            await user.edit(deafen=False, reason=f"Undeafened by {author}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to deafen members."), ephemeral=True)

        embed = ModEmbed.success("Voice Undeafened", f"{user.mention} has been server undeafened.")
        await self._respond(source, embed=embed)
    
    async def _kick(self, source, user: discord.Member, reason: str):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not isinstance(author, discord.Member) or not can_moderate(user, author):
            return await self._respond(
                source,
                embed=ModEmbed.error("Insufficient Permissions", f"You can't disconnect {user.mention} — they're equal to or above you in the role hierarchy."),
                ephemeral=True
            )
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        channel_name = user.voice.channel.name
        try:
            await user.move_to(None, reason=f"{author}: {reason}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to disconnect members."), ephemeral=True)
        
        embed = ModEmbed.success("Disconnected from Voice", 
                                 f"{user.mention} has been disconnected from **{channel_name}**.\n**Reason:** {reason}")
        await self._respond(source, embed=embed)
    
    async def _move(self, source, user: discord.Member, channel: discord.VoiceChannel):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not isinstance(author, discord.Member) or not can_moderate(user, author):
            return await self._respond(
                source,
                embed=ModEmbed.error("Insufficient Permissions", f"You can't move {user.mention} — they're equal to or above you in the role hierarchy."),
                ephemeral=True
            )
        if not user.voice:
            return await self._respond(
                source,
                embed=ModEmbed.error("Not in Voice", f"{user.mention} is not in a voice channel."),
                ephemeral=True
            )
        
        old_channel = user.voice.channel.name
        try:
            await user.move_to(channel, reason=f"Moved by {author}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to move members."), ephemeral=True)
        
        embed = ModEmbed.success("User Moved", 
                                 f"{user.mention} has been moved from **{old_channel}** to **{channel.name}**")
        await self._respond(source, embed=embed)
    
    async def _moveall(self, source, from_channel: discord.VoiceChannel,
                       to_channel: discord.VoiceChannel):
        if not from_channel.members:
            return await self._respond(
                source,
                embed=ModEmbed.error("Empty Channel", f"{from_channel.mention} has no members."),
                ephemeral=True
            )
        
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        
        author = source.user if isinstance(source, discord.Interaction) else source.author

        count = 0
        for member in from_channel.members:
            # Skip members the actor can't moderate (role hierarchy / owners).
            if isinstance(author, discord.Member) and not can_moderate(member, author):
                continue
            try:
                await member.move_to(to_channel, reason=f"Mass move by {author}")
                count += 1
            except Exception:
                pass
        
        embed = ModEmbed.success("Users Moved", 
                                 f"Moved **{count}** users from {from_channel.mention} to {to_channel.mention}")
        await self._respond(source, embed=embed)

    async def _muteall(self, source, channel: discord.VoiceChannel, reason: str):
        await self._set_channel_mute_state(source, channel, True, reason)

    async def _unmuteall(self, source, channel: discord.VoiceChannel, reason: str):
        await self._set_channel_mute_state(source, channel, False, reason)

    async def _set_channel_mute_state(
        self,
        source,
        channel: discord.VoiceChannel,
        mute_state: bool,
        reason: str,
    ):
        if not channel.members:
            return await self._respond(
                source,
                embed=ModEmbed.error("Empty Channel", f"{channel.mention} has no members."),
                ephemeral=True,
            )

        if isinstance(source, discord.Interaction):
            await source.response.defer()

        author = source.user if isinstance(source, discord.Interaction) else source.author
        action_label = "muted" if mute_state else "unmuted"
        action_title = "Voice Muted" if mute_state else "Voice Unmuted"

        changed = 0
        already = 0
        skipped_owner = 0
        failed = 0

        for member in list(channel.members):
            # Skip members the actor can't moderate (role hierarchy / owners).
            if isinstance(author, discord.Member) and not can_moderate(member, author):
                skipped_owner += 1
                continue

            if not member.voice:
                continue

            if member.voice.mute is mute_state:
                already += 1
                continue

            try:
                await member.edit(
                    mute=mute_state,
                    reason=f"Mass VC {action_label} by {author}: {reason}",
                )
                changed += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        summary_lines = [
            f"{channel.mention}: **{changed}** member(s) {action_label}.",
            f"Already {action_label}: **{already}**",
            f"Failed: **{failed}**",
        ]
        if skipped_owner:
            summary_lines.append(f"Skipped protected users: **{skipped_owner}**")
        if reason:
            summary_lines.append(f"Reason: {reason}")

        if failed > 0:
            embed = ModEmbed.warning(action_title, "\n".join(summary_lines))
        else:
            embed = ModEmbed.success(action_title, "\n".join(summary_lines))
        await self._respond(source, embed=embed)

    async def _ban(self, source, user: discord.Member, reason: str):
        if not source.guild:
            return await self._respond(source, embed=ModEmbed.error("Guild Only", "This command can only be used in a server."), ephemeral=True)

        author = source.user if isinstance(source, discord.Interaction) else source.author
        if not isinstance(author, discord.Member) or not can_moderate(user, author):
            return await self._respond(
                source,
                embed=ModEmbed.error("Insufficient Permissions", f"You can't voice-ban {user.mention} — they're equal to or above you in the role hierarchy."),
                ephemeral=True
            )

        if isinstance(source, discord.Interaction):
            await source.response.defer()

        # Disconnect user from voice if currently connected
        if user.voice:
            try:
                await user.move_to(None, reason=f"Voice banned by {author}: {reason}")
            except Exception:
                pass

        # Apply voice ban to all voice channels in the server
        failed = 0
        success = 0
        for channel in source.guild.voice_channels:
            try:
                await channel.set_permissions(
                    user,
                    connect=False,
                    reason=f"Voice banned by {author}: {reason}"
                )
                success += 1
            except Exception:
                failed += 1

        # Also apply to stage channels
        for channel in source.guild.stage_channels:
            try:
                await channel.set_permissions(
                    user,
                    connect=False,
                    reason=f"Voice banned by {author}: {reason}"
                )
                success += 1
            except Exception:
                failed += 1

        # Log to moderation cases if available
        try:
            await self.bot.db.create_case(
                source.guild.id,
                user.id,
                author.id,
                "vcban",
                reason
            )
        except Exception:
            pass

        if failed > 0:
            embed = ModEmbed.warning(
                "Voice Banned (Partial)",
                f"{user.mention} has been voice banned from **{success}** channels.\n"
                f"**Failed:** {failed} channels\n**Reason:** {reason}"
            )
        else:
            embed = ModEmbed.success(
                "Voice Banned",
                f"{user.mention} has been voice banned from all **{success}** voice channels.\n**Reason:** {reason}"
            )
        await self._respond(source, embed=embed)

    async def _unban(self, source, target_id: int):
        """Unban a user from all voice channels by user ID."""
        if not source.guild:
            return await self._respond(source, embed=ModEmbed.error("Guild Only", "This command can only be used in a server."), ephemeral=True)

        if isinstance(source, discord.Interaction):
            await source.response.defer()
            
        author = source.user if isinstance(source, discord.Interaction) else source.author

        # Try to fetch the user object for better display
        target_user = None
        try:
            target_user = await self.bot.fetch_user(target_id)
        except Exception:
            pass

        # Remove voice ban overwrites from all voice channels
        removed = 0
        for channel in source.guild.voice_channels:
            try:
                # Get the permission overwrite for this user
                if target_user:
                    overwrites = channel.overwrites_for(target_user)
                else:
                    # Fallback: check if there's an overwrite by ID
                    overwrites = channel.overwrites.get(discord.Object(id=target_id))
                    if not overwrites:
                        continue
                
                # Only remove if connect is explicitly set to False
                if overwrites and overwrites.connect is False:
                    await channel.set_permissions(
                        target_user if target_user else discord.Object(id=target_id),
                        overwrite=None,
                        reason=f"Voice unbanned by {author}"
                    )
                    removed += 1
            except Exception as e:
                pass

        # Also check stage channels
        for channel in source.guild.stage_channels:
            try:
                if target_user:
                    overwrites = channel.overwrites_for(target_user)
                else:
                    overwrites = channel.overwrites.get(discord.Object(id=target_id))
                    if not overwrites:
                        continue
                
                if overwrites and overwrites.connect is False:
                    await channel.set_permissions(
                        target_user if target_user else discord.Object(id=target_id),
                        overwrite=None,
                        reason=f"Voice unbanned by {author}"
                    )
                    removed += 1
            except Exception as e:
                pass

        # Log to moderation cases if available
        try:
            await self.bot.db.create_case(
                source.guild.id,
                target_id,
                author.id,
                "vcunban",
                "Voice ban removed"
            )
        except Exception:
            pass

        user_mention = target_user.mention if target_user else f"User ID: {target_id}"
        
        if removed > 0:
            embed = ModEmbed.success(
                "Voice Unbanned",
                f"{user_mention} has been voice unbanned. Removed restrictions from **{removed}** channels."
            )
        else:
            embed = ModEmbed.info(
                "No Ban Found",
                f"{user_mention} did not have any voice channel restrictions to remove."
            )
        await self._respond(source, embed=embed)

    async def _verification(self, source, state: Literal["on", "off"]):
        if not source.guild:
            return await self._respond(source, embed=ModEmbed.error("Guild Only", "Use this command in a server."), ephemeral=True)

        enable = state == "on"
        
        if enable:
            # Check if waiting room is configured
            settings = await self.bot.db.get_settings(source.guild.id)
            waiting_id = settings.get("waiting_verify_voice_channel")
            if not waiting_id:
                return await self._respond(
                    source,
                    embed=ModEmbed.error(
                        "Not Configured",
                        "Missing the `waiting-verify` voice channel. Run `/setup` first.",
                    ),
                    ephemeral=True,
                )
            
            waiting = source.guild.get_channel(int(waiting_id))
            if not isinstance(waiting, discord.VoiceChannel):
                return await self._respond(
                    source,
                    embed=ModEmbed.error(
                        "Not Configured",
                        "The waiting-verify channel is invalid. Run `/setup` again.",
                    ),
                    ephemeral=True,
                )

        settings = await self.bot.db.get_settings(source.guild.id)
        settings["voice_verification_enabled"] = enable
        await self.bot.db.update_settings(source.guild.id, settings)

        # If disabling, clear any voice verification state from the Verification cog
        if not enable:
            verification_cog = self.bot.get_cog("Verification")
            if verification_cog:
                gid = source.guild.id
                # Clear pending voice captchas
                voice_pending = [k for k in verification_cog._pending.keys() if k[0] == gid and k[2] == "voice"]
                for k in voice_pending:
                    verification_cog._pending.pop(k, None)
                # Clear voice targets
                for k in [k for k in verification_cog._voice_targets.keys() if k[0] == gid]:
                    verification_cog._voice_targets.pop(k, None)
                # Clear allow once
                for k in [k for k in verification_cog._voice_allow_once.keys() if k[0] == gid]:
                    verification_cog._voice_allow_once.pop(k, None)
                # Clear session verified
                for k in [k for k in verification_cog._voice_session_verified if k[0] == gid]:
                    verification_cog._voice_session_verified.discard(k)

        await self._respond(
            source,
            embed=ModEmbed.success(
                "Updated",
                f"Voice verification is now **{state}**.",
            ),
            ephemeral=True,
        )

    # @commands.command(name="vcmute")
    # @is_mod()
    # async def vcmute(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
    #     """Mute a user in voice"""
    #     await self._mute(ctx, user, reason)

    # @commands.command(name="vcunmute")
    # @is_mod()
    # async def vcunmute(self, ctx: commands.Context, user: discord.Member):
    #     """Unmute a user in voice"""
    #     await self._unmute(ctx, user)

    # @commands.command(name="vcdeafen")
    # @is_mod()
    # async def vcdeafen(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
    #     """Deafen a user in voice"""
    #     await self._deafen(ctx, user, reason)

    # @commands.command(name="vcundeafen")
    # @is_mod()
    # async def vcundeafen(self, ctx: commands.Context, user: discord.Member):
    #     """Undeafen a user in voice"""
    #     await self._undeafen(ctx, user)

    # @commands.command(name="vckick")
    # @is_mod()
    # async def vckick(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
    #     """Kick a user from voice channel"""
    #     await self._kick(ctx, user, reason)

    # @commands.command(name="vcmove")
    # @is_mod()
    # async def vcmove(self, ctx: commands.Context, user: discord.Member, channel: discord.VoiceChannel):
    #     """Move a user to another voice channel"""
    #     await self._move(ctx, user, channel)

    # @commands.command(name="vcmoveall")
    # @is_mod()
    # async def vcmoveall(self, ctx: commands.Context, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel):
    #     """Move all users from one channel to another"""
    #     await self._moveall(ctx, from_channel, to_channel)

    # @commands.command(name="vcban")
    # @is_mod()
    # async def vcban(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
    #     """Ban a user from all voice channels"""
    #     await self._ban(ctx, user, reason)

    # @commands.command(name="vcunban")
    # @is_mod()
    # async def vcunban(self, ctx: commands.Context, user: Union[discord.Member, discord.User, str]):
    #     """Unban a user from voice channels"""
    #     if isinstance(user, (discord.Member, discord.User)):
    #         target_id = user.id
    #     else:
    #         try:
    #             target_id = int(user)
    #         except ValueError:
    #             return await ctx.send(embed=ModEmbed.error("Invalid ID", "Please provide a valid user or user ID."))
    #     await self._unban(ctx, target_id)

    # @commands.command(name="vcverify")
    # @is_admin()
    # async def vcverify(self, ctx: commands.Context, state: Literal["on", "off"]):
    #     """Enable/disable voice verification requirement"""
    #     await self._verification(ctx, state)


async def setup(bot):
    await bot.add_cog(Voice(bot))
