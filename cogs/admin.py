"""
Admin Commands - Bot configuration
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from typing import Optional, Literal, Any
import math
import asyncio
import logging
from utils.embeds import ModEmbed
from utils. checks import is_admin, is_mod
from utils.time_parser import parse_time
from config import Config

logger = logging.getLogger("ModBot.Admin")


def _parse_dt_utc(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _format_entry_count(n: int) -> str:
    return str(max(0, int(n)))


def _giveaway_is_ended(giveaway: dict) -> bool:
    if giveaway.get("ended"):
        return True
    ends_at = _parse_dt_utc(giveaway.get("ends_at"))
    return bool(ends_at and ends_at <= datetime.now(timezone.utc))


def _giveaway_header_text(giveaway: dict, *, ended: bool, winners_text: Optional[str]) -> str:
    prize = (giveaway.get("prize") or "a prize").strip()
    description = (giveaway.get("description") or "").strip()
    winners = int(giveaway.get("winners") or 1)
    ends_at = _parse_dt_utc(giveaway.get("ends_at"))
    host_id = giveaway.get("host_id")

    lines: list[str] = []
    lines.append("**🎉 GIVEAWAY ENDED 🎉**" if ended else "**🎉 GIVEAWAY 🎉**")

    if description:
        lines.append(description)

    lines.append("")
    lines.append(f"**Prize:** {prize}")
    lines.append(f"**Winners:** {winners}")
    if ends_at:
        lines.append(f"**Ends:** <t:{int(ends_at.timestamp())}:R>")
    if host_id:
        lines.append(f"**Host:** <@{int(host_id)}>")
    required_role_id = giveaway.get("required_role_id")
    if required_role_id:
        lines.append(f"**Required Role:** <@&{int(required_role_id)}>")
    bonus_role_id = giveaway.get("bonus_role_id")
    bonus_amount = int(giveaway.get("bonus_amount") or 0)
    if bonus_role_id and bonus_amount > 0:
        lines.append(f"**Bonus Entries:** <@&{int(bonus_role_id)}> (+{bonus_amount})")
    winners_role_id = giveaway.get("winners_role_id")
    if winners_role_id:
        lines.append(f"**Winner Role:** <@&{int(winners_role_id)}>")

    if ended and winners_text:
        lines.append("")
        lines.append(f"**Winner{'s' if ',' in winners_text else ''}:** {winners_text}")

    if not ended:
        lines.append("")
        lines.append("Click 🎉 to enter!")

    return "\n".join(lines).strip()


class GiveawayMessageView(discord.ui.LayoutView):
    def __init__(
        self,
        bot,
        *,
        giveaway: dict,
        entry_count: int,
        ended: bool = False,
        winners_text: Optional[str] = None,
    ):
        super().__init__(timeout=None)
        self.bot = bot

        banner_url = (giveaway.get("banner_url") or "").strip() or None
        thumbnail_url = (giveaway.get("thumbnail_url") or "").strip() or None

        children: list[discord.ui.Item[Any]] = []
        if banner_url:
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url)))

        header = _giveaway_header_text(giveaway, ended=ended, winners_text=winners_text)
        if thumbnail_url:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(thumbnail_url),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(header))

        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        enter_button = discord.ui.Button(
            label=_format_entry_count(entry_count),
            emoji="🎉",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:enter",
            disabled=bool(ended),
        )
        participants_button = discord.ui.Button(
            label="Participants",
            emoji="👥",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:participants",
        )

        async def _enter_cb(interaction: discord.Interaction):
            await GiveawayInteractionView.handle_toggle(self.bot, interaction)

        async def _participants_cb(interaction: discord.Interaction):
            await GiveawayInteractionView.handle_participants(self.bot, interaction)

        enter_button.callback = _enter_cb
        participants_button.callback = _participants_cb

        children.append(discord.ui.ActionRow(enter_button, participants_button))

        self.add_item(discord.ui.Container(*children, accent_color=Config.COLOR_BRAND))


class GiveawayParticipantsView(discord.ui.LayoutView):
    def __init__(
        self,
        bot,
        *,
        requester_id: int,
        giveaway: dict,
        entrants: list[discord.Member],
        weights: list[int],
        page: int = 1,
        per_page: int = 10,
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.requester_id = requester_id
        self.giveaway = giveaway
        self.entrants = entrants
        self.weights = weights
        self.per_page = max(5, min(25, int(per_page)))
        self.page = max(1, int(page))

        self._render()

    def _render(self) -> None:
        self.clear_items()

        prize = (self.giveaway.get("prize") or "a prize").strip()
        total = len(self.entrants)
        pages = max(1, math.ceil(total / self.per_page))
        self.page = min(self.page, pages)

        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        chunk = list(zip(self.entrants, self.weights))[start:end]

        lines: list[str] = []
        for i, (member, weight) in enumerate(chunk, start=start + 1):
            entries_label = "entry" if int(weight) == 1 else "entries"
            lines.append(f"{i}. {member.mention} ({weight} {entries_label})")

        body = (
            f"**Giveaway Participants (Page {self.page}/{pages})**\n"
            f"These are the members that have participated in the giveaway of **{prize}**:\n\n"
            + ("\n".join(lines) if lines else "*No participants yet.*")
            + f"\n\n**Total Participants:** {total}"
        )

        container_children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(body),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        ]

        prev_button = discord.ui.Button(
            label="Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:participants:prev",
            disabled=self.page <= 1,
        )
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:participants:next",
            disabled=self.page >= pages,
        )

        async def _prev_cb(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                return await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            view = GiveawayParticipantsView(
                self.bot,
                requester_id=self.requester_id,
                giveaway=self.giveaway,
                entrants=self.entrants,
                weights=self.weights,
                page=self.page - 1,
                per_page=self.per_page,
            )
            await interaction.response.edit_message(view=view, allowed_mentions=discord.AllowedMentions.none())

        async def _next_cb(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                return await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            view = GiveawayParticipantsView(
                self.bot,
                requester_id=self.requester_id,
                giveaway=self.giveaway,
                entrants=self.entrants,
                weights=self.weights,
                page=self.page + 1,
                per_page=self.per_page,
            )
            await interaction.response.edit_message(view=view, allowed_mentions=discord.AllowedMentions.none())

        prev_button.callback = _prev_cb
        next_button.callback = _next_cb

        container_children.append(discord.ui.ActionRow(prev_button, next_button))
        self.add_item(discord.ui.Container(*container_children, accent_color=Config.COLOR_BRAND))


class GiveawayInteractionView(discord.ui.LayoutView):
    """
    Persistent interaction-only view to handle giveaway buttons after restarts.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

        enter_button = discord.ui.Button(
            label="0",
            emoji="🎉",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:enter",
        )
        participants_button = discord.ui.Button(
            label="Participants",
            emoji="👥",
            style=discord.ButtonStyle.secondary,
            custom_id="modbot:giveaway:participants",
        )

        async def _enter_cb(interaction: discord.Interaction):
            await self.handle_toggle(self.bot, interaction)

        async def _participants_cb(interaction: discord.Interaction):
            await self.handle_participants(self.bot, interaction)

        enter_button.callback = _enter_cb
        participants_button.callback = _participants_cb

        self.add_item(discord.ui.Container(discord.ui.ActionRow(enter_button, participants_button)))

    @staticmethod
    async def _get_giveaway(bot, interaction: discord.Interaction) -> Optional[dict]:
        if not interaction.guild or not interaction.message:
            return None
        giveaway = await bot.db.get_giveaway_by_message_id(interaction.guild.id, interaction.message.id)
        if giveaway:
            return giveaway
        return await bot.db.get_giveaway_by_message_id(interaction.guild.id, interaction.message.id)

    @staticmethod
    async def handle_toggle(bot, interaction: discord.Interaction) -> None:
        import contextlib

        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This can only be used in a server giveaway message."),
                ephemeral=True,
            )

        if interaction.user.bot:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Allowed", "Bots can't enter giveaways."),
                ephemeral=True,
            )

        giveaway = await GiveawayInteractionView._get_giveaway(bot, interaction)
        if not giveaway:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that giveaway in my database yet. Try again."),
                ephemeral=True,
            )

        if _giveaway_is_ended(giveaway):
            entrants = await bot.db.get_giveaway_entries(int(giveaway["id"]))
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(
                    view=GiveawayMessageView(bot, giveaway=giveaway, entry_count=len(entrants), ended=True)
                )
            return await interaction.response.send_message(
                embed=ModEmbed.error("Giveaway Ended", "This giveaway is no longer accepting entries."),
                ephemeral=True,
            )

        required_role_id = giveaway.get("required_role_id")
        if required_role_id and isinstance(interaction.user, discord.Member):
            if not any(r.id == int(required_role_id) for r in interaction.user.roles):
                return await interaction.response.send_message(
                    embed=ModEmbed.error("Not Eligible", "You don't have the required role to enter this giveaway."),
                    ephemeral=True,
                )

        entered = await bot.db.toggle_giveaway_entry(int(giveaway["id"]), interaction.user.id)
        entrants = await bot.db.get_giveaway_entries(int(giveaway["id"]))
        prize = giveaway.get("prize") or "the prize"

        with contextlib.suppress(discord.HTTPException):
            await interaction.message.edit(
                view=GiveawayMessageView(bot, giveaway=giveaway, entry_count=len(entrants), ended=False)
            )

        if entered:
            return await interaction.response.send_message(
                embed=ModEmbed.success("Entered", f"You've successfully entered **{prize}**."),
                ephemeral=True,
            )

        return await interaction.response.send_message(
            embed=ModEmbed.info("Withdrawn", f"You've withdrawn from **{prize}**."),
            ephemeral=True,
        )

    @staticmethod
    async def handle_participants(bot, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This can only be used in a server giveaway message."),
                ephemeral=True,
            )

        giveaway = await GiveawayInteractionView._get_giveaway(bot, interaction)
        if not giveaway:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that giveaway."),
                ephemeral=True,
            )

        admin_cog = bot.get_cog("Admin")
        if not admin_cog:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "Admin cog is not loaded."),
                ephemeral=True,
            )

        entrants, weights = await admin_cog._collect_entrants(interaction.guild, giveaway, interaction.message)
        view = GiveawayParticipantsView(
            bot,
            requester_id=interaction.user.id,
            giveaway=giveaway,
            entrants=entrants,
            weights=weights,
            page=1,
        )
        await interaction.response.send_message(
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(GiveawayInteractionView(self.bot))
        self._giveaway_watcher.start()
        self.spam_tasks: dict[int, asyncio.Task] = {}

    def cog_unload(self):
        try:
            self._giveaway_watcher.cancel()
        except Exception:
            pass
        
        for task in self.spam_tasks.values():
            task.cancel()

    @staticmethod
    def _parse_db_datetime(value: str) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    async def _resolve_giveaway(self, interaction: discord.Interaction, id_value: int):
        giveaway = await self.bot.db.get_giveaway_by_message_id(interaction.guild_id, id_value)
        if not giveaway:
            giveaway = await self.bot.db.get_giveaway_by_id(interaction.guild_id, id_value)
        return giveaway

    async def _fetch_giveaway_message(self, interaction: discord.Interaction, giveaway: dict):
        import contextlib

        msg_id = int(giveaway["message_id"])
        message = None

        giveaway_channel = interaction.guild.get_channel(int(giveaway["channel_id"]))
        if giveaway_channel:
            with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = await giveaway_channel.fetch_message(msg_id)

        if message is None:
            with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = await interaction.channel.fetch_message(msg_id)

        return message

    async def _collect_entrants(self, guild: discord.Guild, giveaway: dict, message: Optional[discord.Message]):
        import contextlib

        entrant_ids = await self.bot.db.get_giveaway_entries(int(giveaway["id"]))
        members: list[discord.Member] = []

        if entrant_ids:
            for uid in entrant_ids:
                member = guild.get_member(int(uid))
                if member is None:
                    with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = await guild.fetch_member(int(uid))
                if member:
                    members.append(member)
        elif message:
            reaction = None
            for r in message.reactions:
                if str(r.emoji) == "🎉":
                    reaction = r
                    break
            if reaction:
                users = [u async for u in reaction.users() if not u.bot]
                for u in users:
                    member = guild.get_member(u.id)
                    if member is None:
                        with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = await guild.fetch_member(u.id)
                    if member:
                        members.append(member)

        required_role_id = giveaway.get("required_role_id")
        bonus_role_id = giveaway.get("bonus_role_id")
        bonus_amount = int(giveaway.get("bonus_amount") or 0)

        entrants: list[discord.Member] = []
        weights: list[int] = []

        for m in members:
            if required_role_id and not any(r.id == int(required_role_id) for r in m.roles):
                continue
            weight = 1
            if bonus_role_id and bonus_amount > 0 and any(r.id == int(bonus_role_id) for r in m.roles):
                weight += bonus_amount
            entrants.append(m)
            weights.append(weight)

        return entrants, weights

    async def _dm_winners(self, giveaway: dict, winners: list[discord.Member]):
        import contextlib

        reward = (giveaway.get("reward") or "").strip()
        if not giveaway.get("dm_winners") and not reward:
            return

        prize = giveaway.get("prize") or "a prize"
        for w in winners:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                msg = f"🎉 You won **{prize}**! (Giveaway ID: `{giveaway['id']}`)"
                if reward:
                    msg += f"\n\n**Reward:**\n```{reward}```"
                await w.send(msg)

    async def _notify_giveaway_staff(
        self,
        guild: discord.Guild,
        giveaway_id: int,
        host: Optional[discord.Member],
        channel: discord.TextChannel,
        message: discord.Message,
        prize: str,
        ends_at: datetime,
    ):
        import contextlib

        recipient_ids: set[int] = set()

        if host:
            recipient_ids.add(host.id)

        if guild.owner_id:
            recipient_ids.add(int(guild.owner_id))

        for oid in getattr(self.bot, "owner_ids", set()) or set():
            recipient_ids.add(int(oid))

        settings = {}
        with contextlib.suppress(Exception):
            settings = await self.bot.db.get_settings(guild.id)

        role_ids: set[int] = set()
        for key in ("admin_roles", "admin_role"):
            value = settings.get(key)
            if isinstance(value, list):
                role_ids.update(int(v) for v in value)
            elif isinstance(value, int):
                role_ids.add(int(value))

        for rid in role_ids:
            role = guild.get_role(rid)
            if role:
                for m in role.members:
                    if not m.bot:
                        recipient_ids.add(m.id)

        embed = discord.Embed(
            title="🎉 Giveaway Created",
            description=(
                f"Giveaway ID: `{giveaway_id}`\n"
                f"Prize: **{prize}**\n"
                f"Channel: {channel.mention}\n"
                f"Ends: <t:{int(ends_at.timestamp())}:R>\n\n"
                f"Message: {message.jump_url}"
            ),
            color=Config.COLOR_INFO,
        )
        embed.add_field(name="Force End", value=f"`/giveaway forceend giveaway_id:{giveaway_id}`", inline=False)
        embed.add_field(name="Reroll", value=f"`/giveaway reroll id:{giveaway_id}`", inline=False)
        embed.add_field(name="Force Winner", value=f"`/giveaway forcewinner giveaway_id:{giveaway_id} winner:@User`", inline=False)

        for uid in recipient_ids:
            user = self.bot.get_user(uid)
            if user is None:
                with contextlib.suppress(discord.NotFound, discord.HTTPException):
                    user = await self.bot.fetch_user(uid)
            if not user:
                continue
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await user.send(embed=embed)

    async def _end_giveaway_record(self, guild: discord.Guild, giveaway: dict, winners: list[discord.Member], message: Optional[discord.Message]):
        import contextlib

        with contextlib.suppress(Exception):
            await self.bot.db.end_giveaway(int(giveaway["id"]))

        winners_role_id = giveaway.get("winners_role_id")
        if winners_role_id:
            role = guild.get_role(int(winners_role_id))
            if role:
                for w in winners:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await w.add_roles(role, reason="Giveaway winner")

        await self._dm_winners(giveaway, winners)

        if not message:
            return

        winners_mentions = ", ".join(w.mention for w in winners) if winners else ""
        entrants = await self.bot.db.get_giveaway_entries(int(giveaway["id"]))
        entry_count = len(entrants)

        with contextlib.suppress(discord.HTTPException):
            await message.edit(
                view=GiveawayMessageView(
                    self.bot,
                    giveaway=giveaway,
                    entry_count=entry_count,
                    ended=True,
                    winners_text=(winners_mentions or None),
                )
            )

        with contextlib.suppress(discord.HTTPException):
            if winners_mentions:
                await message.reply(f"🎉 Congratulations {winners_mentions}! You won!")
            else:
                await message.reply("This giveaway ended with no eligible entries.")

    @tasks.loop(seconds=30)
    async def _giveaway_watcher(self):
        import contextlib
        import random

        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        active = []
        with contextlib.suppress(Exception):
            active = await self.bot.db.get_active_giveaways()
        if not active:
            return

        for giveaway in active:
            ends_at = self._parse_db_datetime(giveaway.get("ends_at"))
            if not ends_at or ends_at > now:
                continue

            guild = self.bot.get_guild(int(giveaway["guild_id"]))
            if not guild:
                continue

            channel = guild.get_channel(int(giveaway["channel_id"]))
            message = None
            if channel:
                with contextlib.suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                    message = await channel.fetch_message(int(giveaway["message_id"]))

            entrants, weights = await self._collect_entrants(guild, giveaway, message)
            winners_count = max(1, min(50, int(giveaway.get("winners") or 1)))

            winners: list[discord.Member] = []
            if entrants:
                winners_count = min(winners_count, len(entrants))
                for _ in range(winners_count):
                    idx = random.choices(range(len(entrants)), weights=weights, k=1)[0]
                    winners.append(entrants.pop(idx))
                    weights.pop(idx)

            await self._end_giveaway_record(guild, giveaway, winners, message)

    # Settings command removed: moved to cogs.settings

    @app_commands.command(name="neutralize", description="🛡️ Send a threat neutralized embed to the channel")
    @app_commands.describe(
        user="The user that was neutralized",
        reason="Reason for neutralization",
        action="Action taken",
        case_id="Case ID (optional)"
    )
    @is_admin()
    async def neutralize(self, interaction: discord.Interaction, user: discord.Member, reason: str, action: str, case_id: Optional[int] = None):
        if not case_id:
            import random
            case_id = random.randint(1000, 9999)
            
        embed = discord.Embed(
            title="🛡️ Threat neutralized",
            color=0x22c55e, # Success Green
        )
        embed.description = (
            f"🎯 **Reason:** {reason}\n\n"
            f"🔨 **Action:** {action}\n\n"
            f"🆔 **Case ID:** #{case_id}"
        )
        # Using a checkmark shield thumbnail
        embed.set_thumbnail(url="https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Check_green_icon.svg/2048px-Check_green_icon.svg.png")
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="modrole", description="🏷️ Add or remove a moderator role")
    @app_commands.describe(action="Add or remove", role="The role")
    @is_admin()
    async def modrole(self, interaction: discord. Interaction,
                      action: Literal['add', 'remove'], role: discord.Role):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        mod_roles = settings.get('mod_roles', [])

        if action == 'add':
            if role. id not in mod_roles: 
                mod_roles.append(role.id)
                settings['mod_roles'] = mod_roles
                await self.bot. db.update_settings(interaction. guild_id, settings)
                embed = ModEmbed.success("Mod Role Added", f"{role.mention} is now a moderator role.")
            else:
                embed = ModEmbed.error("Already Added", f"{role.mention} is already a moderator role.")
        else:
            if role.id in mod_roles:
                mod_roles. remove(role.id)
                settings['mod_roles'] = mod_roles
                await self.bot.db.update_settings(interaction.guild_id, settings)
                embed = ModEmbed.success("Mod Role Removed", f"{role.mention} is no longer a moderator role.")
            else:
                embed = ModEmbed.error("Not Found", f"{role.mention} is not a moderator role.")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="adminrole", description="🏷️ Add or remove an admin role")
    @app_commands.describe(action="Add or remove", role="The role")
    @is_admin()
    async def adminrole(self, interaction: discord.Interaction,
                        action: Literal['add', 'remove'], role: discord.Role):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        admin_roles = settings.get('admin_roles', [])

        if action == 'add':
            if role.id not in admin_roles:
                admin_roles. append(role.id)
                settings['admin_roles'] = admin_roles
                await self.bot.db.update_settings(interaction.guild_id, settings)
                embed = ModEmbed.success("Admin Role Added", f"{role.mention} is now an admin role.")
            else:
                embed = ModEmbed.error("Already Added", f"{role.mention} is already an admin role.")
        else:
            if role.id in admin_roles:
                admin_roles.remove(role.id)
                settings['admin_roles'] = admin_roles
                await self.bot.db.update_settings(interaction.guild_id, settings)
                embed = ModEmbed.success("Admin Role Removed", f"{role.mention} is no longer an admin role.")
            else:
                embed = ModEmbed.error("Not Found", f"{role.mention} is not an admin role.")

        await interaction. response.send_message(embed=embed)

    @app_commands.command(name="ignore", description="🚫 Add or remove ignored channels/roles for AutoMod")
    @app_commands.describe(
        action="Add or remove",
        channel="Channel to ignore",
        role="Role to ignore"
    )
    @is_admin()
    async def ignore(self, interaction: discord.Interaction, action: Literal['add', 'remove'],
                     channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None):
        if not channel and not role:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Missing Input", "Please specify a channel or role. "),
                ephemeral=True
            )

        settings = await self.bot.db.get_settings(interaction.guild_id)
        ignored_channels = settings. get('ignored_channels', [])
        ignored_roles = settings. get('ignored_roles', [])

        result = []

        if channel:
            if action == 'add':
                if channel.id not in ignored_channels:
                    ignored_channels.append(channel.id)
                    result.append(f"Added {channel.mention} to ignored channels")
            else:
                if channel.id in ignored_channels:
                    ignored_channels.remove(channel. id)
                    result.append(f"Removed {channel. mention} from ignored channels")

        if role:
            if action == 'add':
                if role.id not in ignored_roles:
                    ignored_roles.append(role.id)
                    result.append(f"Added {role.mention} to ignored roles")
            else:
                if role.id in ignored_roles:
                    ignored_roles.remove(role.id)
                    result.append(f"Removed {role.mention} from ignored roles")

        settings['ignored_channels'] = ignored_channels
        settings['ignored_roles'] = ignored_roles
        await self.bot.db.update_settings(interaction.guild_id, settings)

        if result: 
            embed = ModEmbed.success("Ignore List Updated", "\n".join(result))
        else:
            embed = ModEmbed.warning("No Changes", "No changes were made.")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="announce", description="📢 Send an announcement to a chosen channel")
    @app_commands.describe(
        channel="Channel to send the announcement in",
        message="Announcement message",
        embed="Send as an embed (true/false)",
    )
    @is_admin()
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        embed: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send(
                embed=ModEmbed.error("Guild Only", "This command can only be used in a server."),
                ephemeral=True,
            )

        me = guild.me
        if me is None and getattr(self.bot, "user", None) is not None:
            me = guild.get_member(self.bot.user.id)

        if me is not None:
            perms = channel.permissions_for(me)
            if not perms.send_messages:
                return await interaction.followup.send(
                    embed=ModEmbed.error("Missing Permissions", f"I can't send messages in {channel.mention}."),
                    ephemeral=True,
                )
            if embed and not perms.embed_links:
                return await interaction.followup.send(
                    embed=ModEmbed.error("Missing Permissions", f"I can't send embeds in {channel.mention}."),
                    ephemeral=True,
                )

        content = (message or "").strip()
        if not content:
            return await interaction.followup.send(
                embed=ModEmbed.error("Missing Message", "Please provide a message to announce."),
                ephemeral=True,
            )

        try:
            if embed:
                announcement_embed = discord.Embed(
                    title="📢 Announcement",
                    description=content[:4096],
                    color=Config.COLOR_INFO,
                    timestamp=datetime.utcnow(),
                )
                announcement_embed.set_footer(text=f"Announced by {interaction.user}")
                await channel.send(embed=announcement_embed)
            else:
                await channel.send(content=content[:2000])
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=ModEmbed.error("Forbidden", f"I don't have permission to post in {channel.mention}."),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                embed=ModEmbed.error("Failed", f"Failed to send announcement: {e}"),
                ephemeral=True,
            )

        await interaction.followup.send(
            embed=ModEmbed.success("Announcement Sent", f"Posted in {channel.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="reset", description="🔄 Reset all bot settings for this server")
    @is_admin()
    async def reset(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚠️ Confirm Reset",
            description="This will reset ALL bot settings for this server.\n\n**This action cannot be undone!**\n\nReact with ✅ to confirm.",
            color=Config.COLOR_WARNING
        )

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        def check(reaction, user):
            return user.id == interaction.user.id and str(reaction.emoji) in ["✅", "❌"]

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=30.0, check=check)

            if str(reaction.emoji) == "✅":
                await self.bot.db.update_settings(interaction.guild_id, {})
                embed = ModEmbed. success("Settings Reset", "All bot settings have been reset.  Run `/setup` to reconfigure.")
                await msg.edit(embed=embed)
            else:
                embed = ModEmbed.info("Cancelled", "Settings reset has been cancelled.")
                await msg. edit(embed=embed)
        except asyncio.TimeoutError:
            embed = ModEmbed.error("Timeout", "Reset cancelled due to timeout.")
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
        except Exception as e:
            logger.error(f"Settings reset failed for guild {interaction.guild_id}: {e}", exc_info=True)
            embed = ModEmbed.error("Reset Failed", "Something went wrong while resetting settings. Please try again.")
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

    giveaway_group = app_commands.Group(name="giveaway", description="Giveaway commands")

    @giveaway_group.command(name="start", description="🎉 Start a giveaway")
    @app_commands.describe(
        duration="How long the giveaway runs (e.g. 2h, 3d, 30m)",
        winners="How many winners get picked (1-50)",
        prize="What you're giving away (text description)",
        reward="Optional reward text/code to DM winners",
        channel="Which channel to post the giveaway in (like #giveaways)",
        description="Custom text/rules shown in the embed (markdown supported)",
        bonusrole="Role that gets extra entries (e.g. @Booster)",
        bonusamount="How many extra entries that bonus role gets (like 5)",
        requiredrole="Only users with this role can enter",
        winnersrole="Auto-assign this role to winners when they win",
        host="Specify who's hosting (defaults to you)",
        banner="URL to a banner shown above the giveaway embed",
        thumbnail="URL to a smaller thumbnail image",
        dmwinners="DM winners automatically with the prize",
    )
    @is_mod()
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        duration: str,
        winners: int,
        prize: str,
        channel: discord.TextChannel,
        reward: Optional[str] = None,
        description: Optional[str] = None,
        bonusrole: Optional[discord.Role] = None,
        bonusamount: Optional[int] = None,
        requiredrole: Optional[discord.Role] = None,
        winnersrole: Optional[discord.Role] = None,
        host: Optional[discord.Member] = None,
        banner: Optional[str] = None,
        thumbnail: Optional[str] = None,
        dmwinners: bool = False,
    ):
        from utils.time_parser import parse_time

        parsed = parse_time(duration)
        if not parsed:
            return await interaction.response.send_message(
                embed=ModEmbed. error("Invalid Duration", "Please use a format like `1d`, `12h`, `30m`"),
                ephemeral=True
            )

        delta, human_duration = parsed
        ends_at = datetime.now(timezone.utc) + delta
        winners = max(1, min(50, winners))

        if bonusamount is not None and bonusrole is None:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Missing Bonus Role", "Provide `bonusrole` when using `bonusamount`."),
                ephemeral=True,
            )

        if bonusrole is not None and bonusamount is None:
            bonusamount = 1

        if bonusamount is not None:
            bonusamount = max(1, min(100, bonusamount))

        if host is None and isinstance(interaction.user, discord.Member):
            host = interaction.user

        reward = (reward or "").strip() or None
        dm_winners = bool(dmwinners or reward)
        preview = {
            "id": 0,
            "guild_id": interaction.guild_id,
            "channel_id": channel.id,
            "message_id": 0,
            "prize": prize,
            "reward": reward,
            "description": description,
            "winners": winners,
            "ends_at": ends_at.isoformat(),
            "ended": 0,
            "host_id": (host.id if host else interaction.user.id),
            "bonus_role_id": (bonusrole.id if bonusrole else None),
            "bonus_amount": (bonusamount or 0),
            "required_role_id": (requiredrole.id if requiredrole else None),
            "winners_role_id": (winnersrole.id if winnersrole else None),
            "thumbnail_url": thumbnail,
            "banner_url": banner,
            "dm_winners": dm_winners,
        }

        await interaction.response.defer(ephemeral=True)
        try:
            msg = await channel.send(
                view=GiveawayMessageView(self.bot, giveaway=preview, entry_count=0, ended=False),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=ModEmbed.error("Forbidden", f"I don't have permission to post in {channel.mention}."),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                embed=ModEmbed.error("Failed", f"Failed to post giveaway: {e}"),
                ephemeral=True,
            )

        # Save giveaway to database
        giveaway_id = await self.bot.db.create_giveaway(
            interaction.guild_id,
            channel.id,
            msg.id,
            prize,
            reward,
            description,
            winners,
            ends_at,
            (host.id if host else interaction.user.id),
            bonus_role_id=(bonusrole.id if bonusrole else None),
            bonus_amount=(bonusamount or 0),
            required_role_id=(requiredrole.id if requiredrole else None),
            winners_role_id=(winnersrole.id if winnersrole else None),
            thumbnail_url=thumbnail,
            banner_url=banner,
            dm_winners=dm_winners,
        )

        await interaction.followup.send(
            embed=ModEmbed.success(
                "Giveaway Started",
                f"Giveaway ID: `{giveaway_id}`\nPosted in {channel.mention}.\n{msg.jump_url}",
            ),
            ephemeral=True,
        )

        try:
            await self._notify_giveaway_staff(interaction.guild, giveaway_id, host, channel, msg, prize, ends_at)
        except Exception:
            pass

    @giveaway_group.command(name="end", description="🛑 End a giveaway (message ID or giveaway ID)")
    @app_commands.describe(id="Giveaway message ID or giveaway ID")
    @is_mod()
    async def giveaway_end(self, interaction: discord.Interaction, id: str):
        import random
        import contextlib

        try:
            id_value = int(id)
        except ValueError:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Invalid ID", "Please provide a valid giveaway ID or message ID."),
                ephemeral=True,
            )

        giveaway = await self._resolve_giveaway(interaction, id_value)
        if not giveaway:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that giveaway."),
                ephemeral=True,
            )

        if giveaway.get("ended"):
            return await interaction.response.send_message(
                embed=ModEmbed.info("Already Ended", f"Giveaway ID: `{giveaway['id']}`"),
                ephemeral=True,
            )

        message = await self._fetch_giveaway_message(interaction, giveaway)
        if message is None:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "Could not fetch the giveaway message."),
                ephemeral=True,
            )

        entrants, weights = await self._collect_entrants(interaction.guild, giveaway, message)
        if not entrants:
            return await interaction.response.send_message(
                embed=ModEmbed.error("No Entries", "No eligible entries found for that giveaway."),
                ephemeral=True,
            )

        winners_count = max(1, min(50, int(giveaway.get("winners") or 1)))
        winners_count = min(winners_count, len(entrants))

        winners: list[discord.Member] = []
        for _ in range(winners_count):
            idx = random.choices(range(len(entrants)), weights=weights, k=1)[0]
            winners.append(entrants.pop(idx))
            weights.pop(idx)

        await self._end_giveaway_record(interaction.guild, giveaway, winners, message)

        winners_mentions = ", ".join(w.mention for w in winners)
        embed = discord.Embed(
            title="🎉 Giveaway Ended!",
            description=f"**Winner{'s' if len(winners) != 1 else ''}:** {winners_mentions}",
            color=Config.COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)

    @giveaway_group.command(name="forceend", description="⏹️ Force end a giveaway by giveaway ID")
    @app_commands.describe(giveaway_id="The giveaway ID")
    @is_mod()
    async def giveaway_forceend(self, interaction: discord.Interaction, giveaway_id: int):
        return await self.giveaway_end(interaction, str(giveaway_id))

    @giveaway_group.command(name="forcewinner", description="🏆 Force a specific winner (ends the giveaway)")
    @app_commands.describe(giveaway_id="The giveaway ID", winner="The user to set as winner")
    @is_mod()
    async def giveaway_forcewinner(
        self, interaction: discord.Interaction, giveaway_id: int, winner: discord.Member
    ):
        giveaway = await self.bot.db.get_giveaway_by_id(interaction.guild_id, int(giveaway_id))
        if not giveaway:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that giveaway."),
                ephemeral=True,
            )

        if winner.bot:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Allowed", "Bots can't be winners."),
                ephemeral=True,
            )

        required_role_id = giveaway.get("required_role_id")
        if required_role_id and not any(r.id == int(required_role_id) for r in winner.roles):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Eligible", "That user doesn't have the required role for this giveaway."),
                ephemeral=True,
            )

        message = await self._fetch_giveaway_message(interaction, giveaway)
        if message is None:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "Could not fetch the giveaway message."),
                ephemeral=True,
            )

        await self._end_giveaway_record(interaction.guild, giveaway, [winner], message)

        embed = ModEmbed.success(
            "Forced Winner", f"Winner set to {winner.mention}. Giveaway ID: `{giveaway['id']}`"
        )
        await interaction.response.send_message(embed=embed)

    @giveaway_group.command(name="reroll", description="🔄 Reroll a giveaway winner (message ID or giveaway ID)")
    @app_commands.describe(id="Giveaway message ID or giveaway ID")
    @is_mod()
    async def giveaway_reroll(self, interaction: discord.Interaction, id: str):
        import random
        import contextlib

        try:
            id_value = int(id)
        except ValueError:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Invalid ID", "Please provide a valid giveaway ID or message ID."),
                ephemeral=True,
            )

        giveaway = await self._resolve_giveaway(interaction, id_value)
        if not giveaway:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "I couldn't find that giveaway."),
                ephemeral=True,
            )

        message = await self._fetch_giveaway_message(interaction, giveaway)
        if message is None:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Found", "Could not fetch the giveaway message."),
                ephemeral=True,
            )

        entrants, weights = await self._collect_entrants(interaction.guild, giveaway, message)
        if not entrants:
            return await interaction.response.send_message(
                embed=ModEmbed.error("No Entries", "No eligible entries found for that giveaway."),
                ephemeral=True,
            )

        winner = random.choices(entrants, weights=weights, k=1)[0]

        winners_role_id = giveaway.get("winners_role_id")
        if winners_role_id:
            role = interaction.guild.get_role(int(winners_role_id))
            if role:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await winner.add_roles(role, reason="Giveaway winner (reroll)")

        await self._dm_winners(giveaway, [winner])

        embed = ModEmbed.success("Giveaway Rerolled", f"New winner: {winner.mention}")
        await interaction.response.send_message(embed=embed, ephemeral=True)




    @app_commands.command(name="spam", description="⚠️ Send a message repeatedly (use /stopspam to stop)")
    @app_commands.describe(
        message="The message to send",
        interval="Seconds between messages",
        duration="Duration (e.g. 10s, 5m, 1h). Default: until stopped."
    )
    @is_admin()
    async def spam(self, interaction: discord.Interaction, message: str, interval: float, duration: str = None):
        target_channel_id = interaction.channel.id
        
        if interval <= 0:
            return await interaction.response.send_message(
                "❌ Interval must be greater than 0.",
                ephemeral=True
            )
        
        if target_channel_id in self.spam_tasks:
            return await interaction.response.send_message(
                "❌ A spam task is already running in this channel. Use `/stopspam` first.",
                ephemeral=True
            )

        duration_seconds = None
        if duration:
            parsed = parse_time(duration)
            if parsed:
                duration_seconds = parsed[0].total_seconds()
            else:
                 try:
                     duration_seconds = float(duration)
                 except ValueError:
                     return await interaction.response.send_message(
                         "❌ Invalid duration format. Use '10s', '5m', '1h' etc.",
                         ephemeral=True
                     )

        await interaction.response.send_message(
            f"✅ Spam started. Sending '{message}' every {interval}s.",
            ephemeral=True
        )

        async def spam_loop():
            try:
                end_time = (asyncio.get_running_loop().time() + duration_seconds) if duration_seconds else None
                
                while True:
                    if end_time and asyncio.get_running_loop().time() >= end_time:
                        break
                    
                    await interaction.channel.send(message)
                    await asyncio.sleep(interval)
                
                await interaction.followup.send(
                    "ℹ️ The spam task has completed.",
                    ephemeral=True
                )
            except asyncio.CancelledError:
                 await interaction.followup.send(
                    "🛑 The spam task was manually stopped.",
                    ephemeral=True
                )
            except Exception as e:
                print(f"Spam error: {e}")
            finally:
                self.spam_tasks.pop(target_channel_id, None)

        task = asyncio.create_task(spam_loop())
        self.spam_tasks[target_channel_id] = task

    @app_commands.command(name="stopspam", description="🛑 Stop the running spam task in this channel")
    @is_admin()
    async def stopspam(self, interaction: discord.Interaction):
        task = self.spam_tasks.get(interaction.channel.id)
        if not task:
            return await interaction.response.send_message(
                "❌ No spam task is running in this channel.",
                ephemeral=True
            )
        
        task.cancel()
        await interaction.response.send_message(
            "✅ Stopped the spam task.",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Admin(bot))
