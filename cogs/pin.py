"""
Sticky pin command: keeps a bot message as the last message in a channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import is_admin
from utils.embeds import ModEmbed
from utils.time_parser import parse_time
from config import Config


_MIN_BUMP_INTERVAL_SECONDS = 1.0


@dataclass
class StickyPin:
    channel_id: int
    content: str
    created_by: int
    created_at: datetime
    expires_at: Optional[datetime]
    as_embed: bool
    message: Optional[discord.Message] = None
    bump_task: Optional[asyncio.Task] = None
    expiry_task: Optional[asyncio.Task] = None
    bump_requested: bool = False
    last_bumped_at: Optional[datetime] = None


class Pin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pins: dict[int, StickyPin] = {}

    def _get_pin(self, channel_id: int) -> Optional[StickyPin]:
        pin = self._pins.get(channel_id)
        if pin is None:
            return None
        if pin.expires_at is not None and datetime.now(timezone.utc) >= pin.expires_at:
            return None
        return pin

    async def _clear_pin(self, channel_id: int, *, delete_message: bool = True) -> None:
        pin = self._pins.pop(channel_id, None)
        if pin is None:
            return

        for task in (pin.bump_task, pin.expiry_task):
            if task is not None and not task.done():
                task.cancel()

        if delete_message and pin.message is not None:
            try:
                await pin.message.delete()
            except Exception:
                pass

    async def _send_pin_message(
        self, channel: discord.abc.Messageable, content: str, *, as_embed: bool
    ) -> discord.Message:
        content = (content or "").strip()
        if as_embed:
            if len(content) > 4096:
                content = content[:4093] + "..."
            embed = discord.Embed(description=content, color=Config.COLOR_INFO)
            return await channel.send(embed=embed)
        else:
            if len(content) > 2000:
                content = content[:1997] + "..."
            return await channel.send(content, use_v2=False)

    async def _bump_pin(self, channel: discord.abc.Messageable, pin: StickyPin) -> None:
        try:
            if pin.message is not None:
                try:
                    await pin.message.delete()
                except Exception:
                    pass

            pin.message = await self._send_pin_message(channel, pin.content, as_embed=pin.as_embed)
            pin.last_bumped_at = datetime.now(timezone.utc)
        except Exception:
            # If we can't bump (missing perms, deleted channel, etc.), stop the pin.
            await self._clear_pin(pin.channel_id, delete_message=False)

    def _schedule_expiry(self, pin: StickyPin) -> None:
        if pin.expires_at is None:
            return

        async def _expiry_worker() -> None:
            try:
                delay = (pin.expires_at - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._clear_pin(pin.channel_id, delete_message=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._clear_pin(pin.channel_id, delete_message=False)

        pin.expiry_task = asyncio.create_task(_expiry_worker())

    @app_commands.command(
        name="pin",
        description="📌 Post a sticky message that stays as the last message (admin only).",
    )
    @app_commands.describe(
        message="Message content to keep at the bottom of the channel",
        duration="Optional duration like 30m, 2h, 1d (auto-stops after this time)",
        embed="Send as an embed (true/false)",
    )
    @is_admin()
    async def pin(
        self,
        interaction: discord.Interaction,
        message: str,
        duration: Optional[str] = None,
        embed: bool = False,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Guild Only", "This command can only be used in a server."),
                ephemeral=True,
            )

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unsupported Channel", "Please run this in a text channel or thread."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        me = interaction.guild.me
        if me is None and getattr(self.bot, "user", None) is not None:
            me = interaction.guild.get_member(self.bot.user.id)

        if me is not None:
            perms = channel.permissions_for(me)
            can_send = perms.send_messages
            if isinstance(channel, discord.Thread):
                can_send = can_send or perms.send_messages_in_threads
            if not can_send:
                return await interaction.followup.send(
                    embed=ModEmbed.error("Missing Permissions", f"I can't send messages in {channel.mention}."),
                    ephemeral=True,
                )
            if embed and not perms.embed_links:
                return await interaction.followup.send(
                    embed=ModEmbed.error("Missing Permissions", f"I can't send embeds in {channel.mention}."),
                    ephemeral=True,
                )

        expires_at: Optional[datetime] = None
        human_duration: Optional[str] = None
        if duration:
            parsed = parse_time(duration)
            if not parsed:
                return await interaction.followup.send(
                    embed=ModEmbed.error(
                        "Invalid Duration",
                        "Use formats like `30m`, `2h`, `1d`, `1h30m`.",
                    ),
                    ephemeral=True,
                )
            delta, human_duration = parsed
            expires_at = datetime.now(timezone.utc) + delta

        # Replace any existing pin in this channel.
        await self._clear_pin(channel.id, delete_message=True)

        pin = StickyPin(
            channel_id=channel.id,
            content=message,
            created_by=interaction.user.id,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            as_embed=bool(embed),
        )
        self._pins[channel.id] = pin

        try:
            pin.message = await self._send_pin_message(channel, message, as_embed=pin.as_embed)
            pin.last_bumped_at = datetime.now(timezone.utc)
        except Exception as e:
            await self._clear_pin(channel.id, delete_message=False)
            return await interaction.followup.send(
                embed=ModEmbed.error("Failed", f"Couldn't send the pinned message: {e}"),
                ephemeral=True,
            )

        self._schedule_expiry(pin)

        expiry_note = f" (expires in **{human_duration}**)" if human_duration else ""
        mode_note = "embed" if embed else "message"
        await interaction.followup.send(
            embed=ModEmbed.success(
                "Pinned",
                f"Sticky pin enabled in {channel.mention}{expiry_note} ({mode_note}).\n"
                "If the channel is very busy, updates may be throttled (about once per second).",
            ),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not message.channel:
            return
        if getattr(self.bot, "user", None) is not None and message.author.id == self.bot.user.id:
            return

        pin = self._get_pin(message.channel.id)
        if pin is None:
            return
        if pin.expires_at is not None and datetime.now(timezone.utc) >= pin.expires_at:
            await self._clear_pin(message.channel.id, delete_message=True)
            return

        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        pin.bump_requested = True
        if pin.bump_task is not None and not pin.bump_task.done():
            return

        async def _bump_worker() -> None:
            try:
                while True:
                    current = self._get_pin(channel.id)
                    if current is None or current is not pin:
                        return
                    if current.expires_at is not None and datetime.now(timezone.utc) >= current.expires_at:
                        await self._clear_pin(channel.id, delete_message=True)
                        return

                    if not current.bump_requested:
                        return

                    now = datetime.now(timezone.utc)
                    last = current.last_bumped_at
                    if last is not None:
                        elapsed = (now - last).total_seconds()
                        if elapsed < _MIN_BUMP_INTERVAL_SECONDS:
                            await asyncio.sleep(_MIN_BUMP_INTERVAL_SECONDS - elapsed)

                    # Re-check pin after sleeping (it may have been cleared/replaced).
                    current = self._get_pin(channel.id)
                    if current is None or current is not pin:
                        return
                    if current.expires_at is not None and datetime.now(timezone.utc) >= current.expires_at:
                        await self._clear_pin(channel.id, delete_message=True)
                        return

                    if not current.bump_requested:
                        return
                    current.bump_requested = False

                    await self._bump_pin(channel, current)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._clear_pin(channel.id, delete_message=False)

        pin.bump_task = asyncio.create_task(_bump_worker())


async def setup(bot: commands.Bot):
    await bot.add_cog(Pin(bot))
