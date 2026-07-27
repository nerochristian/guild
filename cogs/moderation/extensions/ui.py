import discord
import asyncio
import aiohttp
import io
from typing import Optional
from pathlib import Path

from utils.embeds import ModEmbed
from utils.checks import is_bot_owner_id

# Constants
TUTORIAL_VIDEO_URL = "https://cdn.discordapp.com/attachments/1430639019582034013/1454243445207208170/2025-12-26_17-43-35.mp4?ex=6950613f&is=694f0fbf&hm=326c7fa1fc65f79d8585b2084febb11771e531d778f682347a622abe95b22df6"
ADD_EMOJI_TUTORIAL_GIF_URL = "https://s7.ezgif.com/tmp/ezgif-78fb32957f0983d1.gif"
ADD_EMOJI_TUTORIAL_GIF_FILENAME = "addemoji_tutorial.gif"
# Resolving path relative to this file: cogs/moderation_extensions/ui.py -> cogs/moderation_extensions/ -> cogs/ -> root -> assets/
# Original: Path(__file__).resolve().parents[1] / "assets" (from cogs/moderation.py)
# New: Path(__file__).resolve().parents[2] / "assets"
ADD_EMOJI_TUTORIAL_GIF_PATH = Path(__file__).resolve().parents[2] / "assets" / ADD_EMOJI_TUTORIAL_GIF_FILENAME
EMOJI_COMMAND_CHANNEL_ID = None

_TUTORIAL_VIDEO_BYTES: Optional[bytes] = None
_TUTORIAL_VIDEO_LOCK = asyncio.Lock()
_ADD_EMOJI_TUTORIAL_GIF_BYTES: Optional[bytes] = None
_ADD_EMOJI_TUTORIAL_GIF_LOCK = asyncio.Lock()

async def _fetch_tutorial_video_file() -> discord.File:
    global _TUTORIAL_VIDEO_BYTES

    if _TUTORIAL_VIDEO_BYTES is None:
        async with _TUTORIAL_VIDEO_LOCK:
            if _TUTORIAL_VIDEO_BYTES is None:
                max_bytes = 24 * 1024 * 1024
                timeout = aiohttp.ClientTimeout(total=30)
                data = bytearray()

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(TUTORIAL_VIDEO_URL) as response:
                        if response.status != 200:
                            raise ValueError("fetch_failed")
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise ValueError("file_too_large")

                _TUTORIAL_VIDEO_BYTES = bytes(data)

    return discord.File(io.BytesIO(_TUTORIAL_VIDEO_BYTES), filename="tutorial.mp4")


async def _fetch_addemoji_tutorial_gif_file() -> Optional[discord.File]:
    global _ADD_EMOJI_TUTORIAL_GIF_BYTES

    if not ADD_EMOJI_TUTORIAL_GIF_PATH.exists():
        return None

    if _ADD_EMOJI_TUTORIAL_GIF_BYTES is None:
        async with _ADD_EMOJI_TUTORIAL_GIF_LOCK:
            if _ADD_EMOJI_TUTORIAL_GIF_BYTES is None:
                _ADD_EMOJI_TUTORIAL_GIF_BYTES = await asyncio.to_thread(ADD_EMOJI_TUTORIAL_GIF_PATH.read_bytes)

    return discord.File(io.BytesIO(_ADD_EMOJI_TUTORIAL_GIF_BYTES), filename=ADD_EMOJI_TUTORIAL_GIF_FILENAME)


class EmojiApprovalView(discord.ui.View):
    def __init__(
        self,
        cog,
        *,
        requester_id: int,
        emoji_name: str,
        emoji_url: str,
    ) -> None:
        super().__init__(timeout=60 * 60 * 24)  # 24h
        self._cog = cog
        self._requester_id = requester_id
        self._emoji_name = emoji_name
        self._emoji_url = emoji_url
        self._handled = False
        self.message: Optional[discord.Message] = None
        # Note: TextDisplay is not a standard discord.py UI component, assumed custom or new feature.
        # Checking implementation in provided code: `self._text = discord.ui.TextDisplay(...)`
        # Assuming TextDisplay is available or patched.
        # If it fails, I'll fallback or assume existing environment supports it.
        # Based on file read, it was imported from discord.ui, so it must exist in this environment's library version.
        try:
             self._text = discord.ui.TextDisplay(self._render(status="Pending", note="Awaiting admin decision"))
             self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("**Emoji Approval Request**"),
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                    self._text,
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(self._emoji_url)),
                    accent_color=discord.Color.blurple().value,
                )
            )
        except AttributeError:
             # Fallback if TextDisplay/Container/MediaGallery (Discord Components V2) are not standard
             pass

        approve_button = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success)
        reject_button = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger)

        async def _approve(interaction: discord.Interaction):
            return await self.approve(interaction, approve_button)

        async def _reject(interaction: discord.Interaction):
            return await self.reject(interaction, reject_button)

        approve_button.callback = _approve
        reject_button.callback = _reject

        self.add_item(approve_button)
        self.add_item(reject_button)

    def _render(self, *, status: str, note: str) -> str:
        return (
            f"**Requested By:** <@{self._requester_id}>\n"
            f"**Emoji Name:** `:{self._emoji_name}:`\n"
            f"**URL:** {self._emoji_url}\n"
            f"**Status:** {status}\n"
            f"**Note:** {note}"
        )

    def _can_act(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.user
        return bool(
            getattr(member, "guild_permissions", None)
            and member.guild_permissions.administrator
        ) or bool(
            member.id == interaction.guild.owner_id or is_bot_owner_id(member.id)
        )

    async def _disable_all(self) -> None:
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                continue

    async def _update_status(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        note: str,
    ) -> None:
        if hasattr(self, '_text'):
            self._text.content = self._render(status=status, note=note)
        
        await self._disable_all()
        msg = interaction.message or self.message
        if msg:
            try:
                await msg.edit(view=self)
            except Exception:
                pass

    async def on_timeout(self) -> None:
        await self._disable_all()
        if not self.message:
            return
        try:
            if hasattr(self, '_text'):
                 self._text.content = self._render(
                    status="Expired",
                    note="No decision within 24 hours",
                )
            await self.message.edit(view=self)
        except Exception:
            pass

    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._can_act(interaction):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "Administrator required."),
                ephemeral=True,
            )
        if self._handled:
            return await interaction.response.send_message(
                "Already handled.", ephemeral=True
            )
        self._handled = True

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        try:
            # Calls internal method on cog
            emoji = await self._cog._create_emoji_from_url(
                guild=guild,
                name=self._emoji_name,
                url=self._emoji_url,
                reason=f"Approved by {interaction.user} (requested by {self._requester_id})",
            )
        except Exception as e:
            await self._update_status(
                interaction,
                status="Failed",
                note=f"Create failed: `{type(e).__name__}`",
            )
            return await interaction.followup.send(
                embed=ModEmbed.error(
                    "Failed", f"Could not add emoji: `{type(e).__name__}`"
                ),
                ephemeral=True,
            )

        await self._update_status(
            interaction,
            status="Approved",
            note=f"Added {emoji} as `:{emoji.name}:`",
        )
        return await interaction.followup.send(
            embed=ModEmbed.success(
                "Approved", f"Emoji added: {emoji} as `:{emoji.name}:`"
            ),
            ephemeral=True,
        )

    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._can_act(interaction):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "Administrator required."),
                ephemeral=True,
            )
        if self._handled:
            return await interaction.response.send_message(
                "Already handled.", ephemeral=True
            )
        self._handled = True
        await interaction.response.defer(ephemeral=True)

        await self._update_status(
            interaction,
            status="Rejected",
            note=f"Rejected by {interaction.user.mention}",
        )
        return await interaction.followup.send("Rejected.", ephemeral=True)


class AddEmojiTutorialView(discord.ui.View):
    def __init__(self, *, requester_id: int):
        super().__init__(timeout=15 * 60)
        self._requester_id = requester_id

    @discord.ui.button(label="Tutorial", style=discord.ButtonStyle.secondary)
    async def tutorial(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self._requester_id:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not For You", "Only the person who ran `/emoji` can use this button."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            video = await _fetch_tutorial_video_file()
            return await interaction.followup.send(file=video, ephemeral=True)
        except ValueError as e:
            if str(e) == "file_too_large":
                return await interaction.followup.send(
                    content=f"Tutorial video is too large to upload. Here is the link:\n{TUTORIAL_VIDEO_URL}",
                    ephemeral=True,
                )
            return await interaction.followup.send(
                content=f"Couldn't fetch the tutorial video. Here is the link:\n{TUTORIAL_VIDEO_URL}",
                ephemeral=True,
            )
        except Exception:
            return await interaction.followup.send(
                content=f"Couldn't fetch the tutorial video. Here is the link:\n{TUTORIAL_VIDEO_URL}",
                ephemeral=True,
            )
