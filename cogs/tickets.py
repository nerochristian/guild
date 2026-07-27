"""
Ticket System - Support ticket management
"""

import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
from datetime import datetime
from typing import Optional, Literal, Any
import re
import unicodedata
from utils.embeds import ModEmbed
from utils.components_v2 import branded_panel_container
from utils.checks import is_mod, is_bot_owner_id
from utils.guild_branding import (
    get_guild_brand_assets,
    get_ticket_panel_component_emojis,
    resolve_guild_component_emoji,
)
from utils.logging import send_log_embed
from config import Config
import io
from utils.transcript import generate_html_transcript


class TicketPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "Tickets",
        *,
        guild: Optional[discord.Guild] = None,
        category_emojis: Optional[dict[str, object]] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog

        logo_url, banner_url = get_guild_brand_assets(guild)
        category_emojis = category_emojis or {}
        title = f"{guild.name} Tickets" if guild else "Tickets"
        description = (
            "If you need help, click on the option corresponding to the type of ticket you want to open.\n"
            "**Response time may vary due to many factors, so please be patient.**"
        )

        select = discord.ui.Select(
            placeholder="Select a ticket category...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Support",
                    value="general",
                    description="Help with an issue",
                    emoji="🛠️",
                ),
                discord.SelectOption(
                    label="Report",
                    value="report",
                    description="Report a user or problem",
                    emoji="🚨",
                ),
                discord.SelectOption(
                    label="Appeal",
                    value="appeal",
                    description="Appeal a punishment",
                    emoji="📝",
                ),
                discord.SelectOption(
                    label="Other",
                    value="other",
                    description="Anything else",
                    emoji="💬",
                ),
            ],
            custom_id="ticket_panel_select",
        )
        select.options[0].emoji = category_emojis.get("general") or resolve_guild_component_emoji(
            guild,
            "ticket_support",
            "support",
            "ticket",
            "help",
            fallback="🛠️",
        )
        select.options[1].emoji = category_emojis.get("report") or resolve_guild_component_emoji(
            guild,
            "ticket_report",
            "report",
            "alert",
            "siren",
            fallback="🚨",
        )
        select.options[2].emoji = category_emojis.get("appeal") or resolve_guild_component_emoji(
            guild,
            "ticket_appeal",
            "appeal",
            "note",
            "pencil",
            fallback="📝",
        )
        select.options[3].emoji = category_emojis.get("other") or resolve_guild_component_emoji(
            guild,
            "ticket_other",
            "other",
            "chat",
            "message",
            fallback="💬",
        )

        async def _select_cb(interaction: discord.Interaction):
            await self._on_select(interaction, select)

        select.callback = _select_cb

        container = branded_panel_container(
            title=title,
            description=description,
            banner_url=banner_url,
            logo_url=logo_url,
            accent_color=Config.COLOR_BRAND,
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(discord.ui.ActionRow(select))
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not interaction.guild or not interaction.guild_id:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "Tickets can only be created in a server."),
                ephemeral=True,
            )

        category = (select.values[0] if select.values else "general").strip().lower()
        await interaction.response.send_modal(TicketDetailsModal(self.cog, category=category))


def _slugify_display_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "user"

    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"[0-9]+", "", name)
    name = re.sub(r"[^a-z\\s-]", "", name)
    name = re.sub(r"\\s+", "-", name).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    return name or "user"


def _unique_channel_name(base: str, existing_names: set[str]) -> str:
    base = (base or "").strip().lower()
    if base and base not in existing_names:
        return base

    alphabet = "abcdefghijklmnopqrstuvwxyz"
    suffix = 0
    while True:
        suffix += 1
        x = suffix
        letters = []
        while x > 0:
            x -= 1
            letters.append(alphabet[x % 26])
            x //= 26
        candidate = f"{base}-" + "".join(reversed(letters))
        if candidate not in existing_names:
            return candidate


def _ticket_category_label(category: str) -> str:
    category = (category or "general").strip().lower()
    return {
        "general": "Support",
        "report": "Report",
        "appeal": "Appeal",
        "other": "Other",
    }.get(category, "Support")


class TicketDetailsModal(discord.ui.Modal):
    def __init__(self, cog: "Tickets", *, category: str):
        self.cog = cog
        self.category = (category or "general").strip().lower()

        title = f"{_ticket_category_label(self.category)} Ticket"
        super().__init__(title=title, timeout=300)

        if self.category == "report":
            self.reported = discord.ui.TextInput(
                label="Who are you reporting? (name/ID)",
                placeholder="User#0000 or 1234567890",
                required=True,
                max_length=100,
            )
            self.reason = discord.ui.TextInput(
                label="Reason",
                placeholder="Explain what happened...",
                required=True,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )
            self.evidence = discord.ui.TextInput(
                label="Evidence (links) (optional)",
                placeholder="Links to screenshots/videos/messages",
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )
            self.add_item(self.reported)
            self.add_item(self.reason)
            self.add_item(self.evidence)
        elif self.category == "appeal":
            self.punishment = discord.ui.TextInput(
                label="What are you appealing? (ban/mute/etc.)",
                placeholder="e.g. mute",
                required=True,
                max_length=60,
            )
            self.why = discord.ui.TextInput(
                label="Why should it be lifted?",
                placeholder="Explain why you should be unpunished...",
                required=True,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )
            self.add_item(self.punishment)
            self.add_item(self.why)
        else:
            self.details = discord.ui.TextInput(
                label="How can we help you?",
                placeholder="Describe your issue...",
                required=True,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )
            self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.guild_id:
            await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "Tickets can only be created in a server."),
                ephemeral=True,
            )
            return

        details = ""
        if self.category == "report":
            evidence = (getattr(self, "evidence").value or "").strip()
            details = (
                f"**Reported:** {getattr(self, 'reported').value.strip()}\n"
                f"**Reason:** {getattr(self, 'reason').value.strip()}"
            )
            if evidence:
                details += f"\n**Evidence:** {evidence}"
        elif self.category == "appeal":
            details = (
                f"**Punishment:** {getattr(self, 'punishment').value.strip()}\n"
                f"**Appeal:** {getattr(self, 'why').value.strip()}"
            )
        else:
            details = getattr(self, "details").value.strip()

        await interaction.response.defer(ephemeral=True)
        await self.cog._create_ticket_from_panel(interaction, category=self.category, details=details)


class TicketThreadPanel(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "Tickets",
        *,
        opener: discord.Member,
        category: str,
        details: str,
        claimed_by: Optional[int] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.opener_id = opener.id
        self.category = (category or "general").strip().lower()
        self.details = (details or "").strip()
        self.claimed_by = claimed_by

        opener_avatar = None
        try:
            opener_avatar = str(opener.display_avatar.url)
        except Exception:
            opener_avatar = None

        category_label = _ticket_category_label(self.category)

        assigned = (
            f"<@{claimed_by}> ({claimed_by})" if claimed_by else "*Unassigned*"
        )

        self.assigned_text = discord.ui.TextDisplay(f"**Assigned staff**\n{assigned}")

        # Main ticket card
        container_children: list[discord.ui.Item[Any]] = []
        header_text = (
            f"**{category_label} Ticket**\n"
            "Please wait until one of our support team members can help you.\n"
            "**Response time may vary due to many factors, so please be patient.**"
        )
        if opener_avatar:
            container_children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header_text),
                    accessory=discord.ui.Thumbnail(opener_avatar),
                )
            )
        else:
            container_children.append(discord.ui.TextDisplay(header_text))
        container_children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container_children.append(self.assigned_text)
        container_children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container_children.append(
            discord.ui.TextDisplay(
                f"❓ **How can we help you?**\n```{(self.details or 'No details provided.').strip()}```"
            )
        )

        close_button = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            custom_id="ticket_close_v2",
        )
        claim_button = discord.ui.Button(
            label="Assign me",
            style=discord.ButtonStyle.success,
            custom_id="ticket_claim_v2",
        )
        self.close_button = close_button
        self.claim_button = claim_button

        async def _close_cb(interaction: discord.Interaction):
            await self.cog._handle_ticket_close_button(interaction, panel=self)

        async def _claim_cb(interaction: discord.Interaction):
            await self.cog._handle_ticket_claim_button(interaction, panel=self)

        close_button.callback = _close_cb
        claim_button.callback = _claim_cb

        container_children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container_children.append(discord.ui.ActionRow(close_button, claim_button))

        self.container = discord.ui.Container(*container_children, accent_color=Config.COLOR_BRAND)
        self.add_item(self.container)

    def set_claimed_by(self, user_id: int) -> None:
        self.claimed_by = user_id
        self.assigned_text.content = f"**Assigned staff**\n<@{user_id}> ({user_id})"


class TicketCloseButton(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Close Ticket", style=discord.ButtonStyle. danger, emoji="🔒", custom_id="ticket_close")
    async def close_ticket(self, interaction: discord. Interaction, button: ui.Button):
        await interaction.response. send_message("Closing ticket in 5 seconds...", ephemeral=True)
        await interaction.channel.send(embed=ModEmbed.warning("Ticket Closing", "This ticket will be closed in 5 seconds... "))
        
        import asyncio
        await asyncio.sleep(5)
        
        # Generate transcript
        messages = [m async for m in interaction.channel.history(limit=None, oldest_first=True)]
        transcript_file = generate_html_transcript(interaction.guild, interaction.channel, messages)
        transcript_file.seek(0)
        
        # Get ticket info
        ticket = await interaction.client.db.get_ticket(interaction.channel.id)
        if ticket:
            await interaction.client.db.close_ticket(interaction.channel.id)
            
            # Send transcript to log channel
            settings = await interaction.client.db. get_settings(interaction.guild_id)
            if settings.get('ticket_log_channel'):
                log_channel = interaction.guild.get_channel(settings['ticket_log_channel'])
                if log_channel:
                    embed = discord.Embed(
                        title=f"🎫 Ticket #{ticket['ticket_number']} Closed",
                        color=Config.COLOR_INFO,
                        timestamp=datetime. utcnow()
                    )
                    creator = interaction.guild.get_member(ticket['user_id'])
                    embed. add_field(name="Created By", value=creator.mention if creator else f"ID:  {ticket['user_id']}", inline=True)
                    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
                    embed.add_field(name="Category", value=ticket['category'], inline=True)
                    
                    file = discord.File(transcript_file, filename=f"ticket-{ticket['ticket_number']}.html")
                    await send_log_embed(log_channel, embed, file=file)
        
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

class TicketCreateButton(ui.View):
    def __init__(self, cog: Optional["Tickets"] = None):
        super().__init__(timeout=None)
        self.cog = cog
    
    @ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket_create")
    async def create_ticket(self, interaction: discord.Interaction, button: ui. Button):
        if not self.cog:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "Ticket system is still starting up. Try again in a moment."),
                ephemeral=True,
            )
        await interaction.response.send_modal(TicketDetailsModal(self.cog, category="general"))

class Tickets(commands.Cog):
    ticket_group = app_commands.Group(name="ticket", description="Ticket management commands")

    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(TicketCreateButton(self))
        self.bot.add_view(TicketCloseButton())
        self.bot.add_view(TicketPanelView(self))

    @staticmethod
    def _normalize_ticket_category(category: Optional[str]) -> str:
        value = (category or "general").strip().lower()
        if value not in {"general", "report", "appeal", "other"}:
            return "general"
        return value

    @staticmethod
    def _normalize_ticket_channel_name(name: str) -> Optional[str]:
        candidate = (name or "").strip().lower()
        candidate = re.sub(r"[^a-z0-9-]", "-", candidate)
        candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
        return candidate or None

    @staticmethod
    def _close_delay_seconds() -> int:
        try:
            return max(1, int(getattr(Config, "TICKET_CLOSE_DELAY", 5)))
        except Exception:
            return 5

    @staticmethod
    def _ticket_file_name(ticket_number: Any) -> str:
        return f"ticket-{ticket_number}.html"

    async def _is_ticket_staff(self, member: discord.Member, settings: Optional[dict] = None) -> bool:
        if is_bot_owner_id(member.id):
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return True

        if settings is None:
            settings = await self.bot.db.get_settings(member.guild.id)

        role_ids: set[int] = set()
        for key in (
            "ticket_support_role",
            "staff_role",
            "owner_role",
            "manager_role",
            "admin_role",
            "supervisor_role",
            "senior_mod_role",
            "mod_role",
            "trial_mod_role",
        ):
            raw = settings.get(key)
            if isinstance(raw, int) and raw > 0:
                role_ids.add(raw)

        for key in ("admin_roles", "mod_roles"):
            raw = settings.get(key)
            if isinstance(raw, list):
                for rid in raw:
                    if isinstance(rid, int) and rid > 0:
                        role_ids.add(rid)

        if not role_ids:
            return False

        member_role_ids = {r.id for r in member.roles}
        return bool(role_ids & member_role_ids)

    async def _build_ticket_transcript(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> io.BytesIO:
        messages = [m async for m in channel.history(limit=None, oldest_first=True)]
        transcript_file = generate_html_transcript(guild, channel, messages)
        transcript_file.seek(0)
        return transcript_file

    async def _send_ticket_close_log(
        self,
        guild: discord.Guild,
        ticket: dict,
        closer: discord.Member,
        transcript_file: io.BytesIO,
        *,
        reason: Optional[str] = None,
    ) -> None:
        settings = await self.bot.db.get_settings(guild.id)
        log_channel_id = settings.get("ticket_log_channel")
        if not isinstance(log_channel_id, int):
            return

        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            return

        embed = discord.Embed(
            title=f"Ticket #{ticket.get('ticket_number', '?')} Closed",
            color=Config.COLOR_BRAND,
            timestamp=datetime.utcnow(),
        )
        creator = guild.get_member(ticket.get("user_id", 0))
        embed.add_field(
            name="Created By",
            value=creator.mention if creator else f"ID: {ticket.get('user_id', 'unknown')}",
            inline=True,
        )
        embed.add_field(name="Closed By", value=closer.mention, inline=True)
        embed.add_field(name="Category", value=ticket.get("category", "general"), inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        transcript_file.seek(0)
        file = discord.File(
            transcript_file,
            filename=self._ticket_file_name(ticket.get("ticket_number", "transcript")),
        )
        await send_log_embed(log_channel, embed, file=file)

    async def _finalize_ticket_close(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel,
        closer: discord.Member,
        reason: Optional[str] = None,
    ) -> tuple[bool, str]:
        ticket = await self.bot.db.get_ticket(channel.id)
        if not ticket:
            return False, "This channel is not a ticket."

        transcript_file = await self._build_ticket_transcript(guild, channel)
        await self.bot.db.close_ticket(channel.id)
        await self._send_ticket_close_log(guild, ticket, closer, transcript_file, reason=reason)

        delete_reason = f"Ticket closed by {closer}"
        if reason:
            delete_reason = f"{delete_reason}: {reason}"

        try:
            await channel.delete(reason=delete_reason)
        except Exception as exc:
            return False, f"Ticket closed in database, but channel deletion failed: {exc}"

        return True, ""

    async def _create_ticket_channel(
        self,
        *,
        guild: discord.Guild,
        opener: discord.Member,
        category: str,
        details: str,
    ) -> tuple[Optional[discord.TextChannel], Optional[str]]:
        settings = await self.bot.db.get_settings(guild.id)
        category_id = settings.get("ticket_category")
        if not isinstance(category_id, int):
            return None, "Ticket system is not set up. Run `/setup` first."

        ticket_category = guild.get_channel(category_id)
        if not isinstance(ticket_category, discord.CategoryChannel):
            return None, "Ticket category is missing. Run `/setup` again."

        topic_marker = f"({opener.id})"
        for existing_channel in ticket_category.channels:
            if getattr(existing_channel, "topic", None) and topic_marker in (existing_channel.topic or ""):
                return None, f"You already have an open ticket: {existing_channel.mention}"

        normalized_category = self._normalize_ticket_category(category)
        category_label = _ticket_category_label(normalized_category)
        display_slug = _slugify_display_name(getattr(opener, "display_name", str(opener)))
        base_name = f"{category_label.lower()}-{display_slug}"
        existing_names = {c.name for c in ticket_category.channels}
        channel_name = _unique_channel_name(base_name, existing_names)

        ticket_number = await self.bot.db.get_next_ticket_number(guild.id)

        bot_member = guild.me
        if bot_member is None and self.bot.user:
            bot_member = guild.get_member(self.bot.user.id)

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            )

        role_ids: set[int] = set()
        for key in (
            "ticket_support_role",
            "staff_role",
            "owner_role",
            "manager_role",
            "admin_role",
            "supervisor_role",
            "senior_mod_role",
            "mod_role",
            "trial_mod_role",
        ):
            raw = settings.get(key)
            if isinstance(raw, int) and raw > 0:
                role_ids.add(raw)
        for key in ("admin_roles", "mod_roles"):
            raw = settings.get(key)
            if isinstance(raw, list):
                for rid in raw:
                    if isinstance(rid, int) and rid > 0:
                        role_ids.add(rid)

        for rid in role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )

        channel = await guild.create_text_channel(
            channel_name,
            category=ticket_category,
            overwrites=overwrites,
            topic=f"Ticket for {opener} ({opener.id}) | Category: {normalized_category}",
        )

        await self.bot.db.create_ticket(
            guild.id,
            channel.id,
            opener.id,
            ticket_number,
            normalized_category,
            details=details,
        )

        panel_view = TicketThreadPanel(
            self,
            opener=opener,
            category=normalized_category,
            details=details,
            claimed_by=None,
        )
        panel_message = await channel.send(view=panel_view)
        try:
            await panel_message.pin(reason="Ticket panel")
        except Exception:
            pass

        return channel, None

    async def _create_ticket_from_panel(self, interaction: discord.Interaction, *, category: str, details: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                embed=ModEmbed.error("Unavailable", "Tickets can only be created in a server."),
                ephemeral=True,
            )
            return

        channel, error = await self._create_ticket_channel(
            guild=interaction.guild,
            opener=interaction.user,
            category=category,
            details=(details or "").strip() or "No details provided.",
        )
        if error:
            await interaction.followup.send(embed=ModEmbed.error("Ticket Error", error), ephemeral=True)
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="Ticket Created",
                description=f"Your ticket has been created: {channel.mention}",
                color=Config.COLOR_BRAND,
            ),
            ephemeral=True,
        )

    async def _send_ticket_panel_to_channel(self, channel: discord.TextChannel, guild: discord.Guild) -> None:
        category_emojis = await get_ticket_panel_component_emojis(guild)
        await channel.send(view=TicketPanelView(self, guild=guild, category_emojis=category_emojis))

    async def _handle_ticket_claim_button(self, interaction: discord.Interaction, *, panel: TicketThreadPanel) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This can only be used in a server.", ephemeral=True)

        settings = await self.bot.db.get_settings(interaction.guild_id)
        if not await self._is_ticket_staff(interaction.user, settings):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("This channel is not a ticket.", ephemeral=True)

        claimed_by = ticket.get("claimed_by")
        if claimed_by:
            return await interaction.response.send_message(
                f"This ticket is already claimed by <@{claimed_by}>.",
                ephemeral=True,
            )

        await self.bot.db.claim_ticket(interaction.channel.id, interaction.user.id)

        panel.set_claimed_by(interaction.user.id)
        panel.claim_button.disabled = True
        try:
            await interaction.message.edit(view=panel)
        except Exception:
            pass

        await interaction.response.send_message("Ticket claimed.", ephemeral=True)

    async def _handle_ticket_close_button(self, interaction: discord.Interaction, *, panel: TicketThreadPanel) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This can only be used in a server.", ephemeral=True)

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("This channel is not a ticket.", ephemeral=True)

        settings = await self.bot.db.get_settings(interaction.guild_id)
        is_staff = await self._is_ticket_staff(interaction.user, settings)
        if not is_staff and interaction.user.id != ticket.get("user_id", panel.opener_id):
            return await interaction.response.send_message(
                "Only staff or the ticket creator can close this ticket.",
                ephemeral=True,
            )

        delay = self._close_delay_seconds()
        await interaction.response.send_message(f"Closing ticket in {delay} seconds...", ephemeral=True)
        await interaction.channel.send(
            embed=discord.Embed(
                title="Ticket Closing",
                description=f"This ticket will be closed in {delay} seconds...",
                color=Config.COLOR_BRAND,
            )
        )

        await asyncio.sleep(delay)
        ok, error = await self._finalize_ticket_close(
            guild=interaction.guild,
            channel=interaction.channel,
            closer=interaction.user,
            reason=None,
        )
        if not ok:
            try:
                await interaction.followup.send(embed=ModEmbed.error("Close Failed", error), ephemeral=True)
            except Exception:
                pass

    @ticket_group.command(name="create", description="Create a support ticket")
    @app_commands.describe(category="Ticket category")
    async def ticket_create(
        self,
        interaction: discord.Interaction,
        category: Literal["general", "report", "appeal", "other"] = "general",
    ) -> None:
        if not interaction.guild:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "Tickets can only be created in a server."),
                ephemeral=True,
            )
        await interaction.response.send_modal(
            TicketDetailsModal(self, category=self._normalize_ticket_category(str(category)))
        )

    @ticket_group.command(name="close", description="Close the current ticket")
    @app_commands.describe(reason="Reason for closing the ticket")
    async def ticket_close(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."),
                ephemeral=True,
            )

        settings = await self.bot.db.get_settings(interaction.guild_id)
        is_staff = await self._is_ticket_staff(interaction.user, settings)
        if not is_staff and interaction.user.id != ticket.get("user_id"):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "Only ticket staff or the ticket creator can close this ticket."),
                ephemeral=True,
            )

        close_reason = (reason or "No reason provided").strip()
        delay = self._close_delay_seconds()
        await interaction.response.send_message(
            embed=ModEmbed.warning(
                "Closing Ticket",
                f"This ticket will be closed in {delay} seconds.\n**Reason:** {close_reason}",
            )
        )

        await asyncio.sleep(delay)
        ok, error = await self._finalize_ticket_close(
            guild=interaction.guild,
            channel=interaction.channel,
            closer=interaction.user,
            reason=close_reason,
        )
        if not ok:
            await interaction.followup.send(embed=ModEmbed.error("Close Failed", error), ephemeral=True)

    @ticket_group.command(name="add", description="Add a user to this ticket")
    @app_commands.describe(user="User to add to this ticket")
    async def ticket_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
        if not await self._is_ticket_staff(interaction.user):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action."),
                ephemeral=True,
            )

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."),
                ephemeral=True,
            )

        await interaction.channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            attach_files=True,
            read_message_history=True,
        )
        await interaction.response.send_message(
            embed=ModEmbed.success("User Added", f"{user.mention} has been added to this ticket.")
        )

    @ticket_group.command(name="remove", description="Remove a user from this ticket")
    @app_commands.describe(user="User to remove from this ticket")
    async def ticket_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
        if not await self._is_ticket_staff(interaction.user):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action."),
                ephemeral=True,
            )

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."),
                ephemeral=True,
            )
        if user.id == ticket.get("user_id"):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Cannot Remove", "You cannot remove the ticket creator."),
                ephemeral=True,
            )

        await interaction.channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message(
            embed=ModEmbed.success("User Removed", f"{user.mention} has been removed from this ticket.")
        )

    @ticket_group.command(name="rename", description="Rename this ticket channel")
    @app_commands.describe(name="New ticket channel name")
    async def ticket_rename(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
        if not await self._is_ticket_staff(interaction.user):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action."),
                ephemeral=True,
            )

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."),
                ephemeral=True,
            )

        normalized = self._normalize_ticket_channel_name(name)
        if not normalized:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Invalid Name", "Please provide a valid channel name."),
                ephemeral=True,
            )

        await interaction.channel.edit(name=normalized)
        await interaction.response.send_message(
            embed=ModEmbed.success("Ticket Renamed", f"Ticket renamed to **{normalized}**")
        )

    @ticket_group.command(name="transcript", description="Generate a transcript for this ticket")
    async def ticket_transcript(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
        if not await self._is_ticket_staff(interaction.user):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action."),
                ephemeral=True,
            )

        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."),
                ephemeral=True,
            )

        await interaction.response.defer()
        transcript_file = await self._build_ticket_transcript(interaction.guild, interaction.channel)
        file = discord.File(transcript_file, filename=f"transcript-{interaction.channel.id}.html")
        await interaction.followup.send(
            embed=ModEmbed.success("Transcript Generated", "Here is the transcript of this ticket."),
            file=file,
        )

    @ticket_group.command(name="panel", description="Post the ticket creation panel")
    @is_mod()
    async def ticket_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a text channel."),
                ephemeral=True,
            )
        await self._send_ticket_panel_to_channel(interaction.channel, interaction.guild)
        await interaction.response.send_message(
            embed=ModEmbed.success("Panel Created", "Ticket panel has been created."),
            ephemeral=True,
        )

    @app_commands.command(name="ticketpanel", description="Post the ticket panel (legacy alias)")
    @is_mod()
    async def ticketpanel_alias(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "This command can only be used in a text channel."),
                ephemeral=True,
            )
        await self._send_ticket_panel_to_channel(interaction.channel, interaction.guild)
        await interaction.response.send_message(
            embed=ModEmbed.success("Panel Created", "Ticket panel has been created."),
            ephemeral=True,
        )

    @commands.group(name="ticket", aliases=["tickets"], invoke_without_command=True)
    @commands.guild_only()
    async def ticket_prefix(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand:
            return
        await ctx.send(
            "Ticket commands: `,ticket create`, `,ticket close`, `,ticket add`, "
            "`,ticket remove`, `,ticket rename`, `,ticket transcript`, `,ticket panel`"
        )

    @ticket_prefix.command(name="create", aliases=["open", "new"])
    async def ticket_create_prefix(
        self,
        ctx: commands.Context,
        category: Optional[str] = "general",
        *,
        details: Optional[str] = None,
    ) -> None:
        if not isinstance(ctx.author, discord.Member) or not ctx.guild:
            return

        requested_category = (category or "general").strip().lower()
        final_category = self._normalize_ticket_category(requested_category)
        final_details = (details or "").strip()
        if requested_category not in {"general", "report", "appeal", "other"}:
            final_details = f"{(category or '').strip()} {final_details}".strip()

        channel, error = await self._create_ticket_channel(
            guild=ctx.guild,
            opener=ctx.author,
            category=final_category,
            details=final_details or "No details provided.",
        )
        if error:
            return await ctx.send(embed=ModEmbed.error("Ticket Error", error))
        await ctx.send(embed=ModEmbed.success("Ticket Created", f"Your ticket has been created: {channel.mention}"))

    @ticket_prefix.command(name="close", aliases=["c"])
    async def ticket_close_prefix(self, ctx: commands.Context, *, reason: Optional[str] = None) -> None:
        if not isinstance(ctx.author, discord.Member) or not ctx.guild:
            return

        ticket = await self.bot.db.get_ticket(ctx.channel.id)
        if not ticket:
            return await ctx.send(embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."))

        settings = await self.bot.db.get_settings(ctx.guild.id)
        is_staff = await self._is_ticket_staff(ctx.author, settings)
        if not is_staff and ctx.author.id != ticket.get("user_id"):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "Only ticket staff or the ticket creator can close this ticket.")
            )

        close_reason = (reason or "No reason provided").strip()
        delay = self._close_delay_seconds()
        await ctx.send(
            embed=ModEmbed.warning(
                "Closing Ticket",
                f"This ticket will be closed in {delay} seconds.\n**Reason:** {close_reason}",
            )
        )
        await asyncio.sleep(delay)
        ok, error = await self._finalize_ticket_close(
            guild=ctx.guild,
            channel=ctx.channel,
            closer=ctx.author,
            reason=close_reason,
        )
        if not ok:
            await ctx.send(embed=ModEmbed.error("Close Failed", error))

    @ticket_prefix.command(name="add")
    async def ticket_add_prefix(self, ctx: commands.Context, user: discord.Member) -> None:
        if not isinstance(ctx.author, discord.Member):
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )

        ticket = await self.bot.db.get_ticket(ctx.channel.id)
        if not ticket:
            return await ctx.send(embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."))

        await ctx.channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            attach_files=True,
            read_message_history=True,
        )
        await ctx.send(embed=ModEmbed.success("User Added", f"{user.mention} has been added to this ticket."))

    @ticket_prefix.command(name="remove")
    async def ticket_remove_prefix(self, ctx: commands.Context, user: discord.Member) -> None:
        if not isinstance(ctx.author, discord.Member):
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )

        ticket = await self.bot.db.get_ticket(ctx.channel.id)
        if not ticket:
            return await ctx.send(embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."))
        if user.id == ticket.get("user_id"):
            return await ctx.send(embed=ModEmbed.error("Cannot Remove", "You cannot remove the ticket creator."))

        await ctx.channel.set_permissions(user, overwrite=None)
        await ctx.send(embed=ModEmbed.success("User Removed", f"{user.mention} has been removed from this ticket."))

    @ticket_prefix.command(name="rename")
    async def ticket_rename_prefix(self, ctx: commands.Context, *, name: str) -> None:
        if not isinstance(ctx.author, discord.Member):
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )

        ticket = await self.bot.db.get_ticket(ctx.channel.id)
        if not ticket:
            return await ctx.send(embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."))

        normalized = self._normalize_ticket_channel_name(name)
        if not normalized:
            return await ctx.send(embed=ModEmbed.error("Invalid Name", "Please provide a valid channel name."))

        await ctx.channel.edit(name=normalized)
        await ctx.send(embed=ModEmbed.success("Ticket Renamed", f"Ticket renamed to **{normalized}**"))

    @ticket_prefix.command(name="transcript", aliases=["logs"])
    async def ticket_transcript_prefix(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.author, discord.Member) or not ctx.guild:
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )

        ticket = await self.bot.db.get_ticket(ctx.channel.id)
        if not ticket:
            return await ctx.send(embed=ModEmbed.error("Not a Ticket", "This channel is not a ticket."))

        transcript_file = await self._build_ticket_transcript(ctx.guild, ctx.channel)
        file = discord.File(transcript_file, filename=f"transcript-{ctx.channel.id}.html")
        await ctx.send(embed=ModEmbed.success("Transcript Generated", "Here is the transcript of this ticket."), file=file)

    @ticket_prefix.command(name="panel")
    async def ticket_panel_prefix(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.author, discord.Member) or not ctx.guild:
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send(embed=ModEmbed.error("Unavailable", "This command can only be used in text channels."))

        await self._send_ticket_panel_to_channel(ctx.channel, ctx.guild)
        await ctx.send(embed=ModEmbed.success("Panel Created", "Ticket panel has been created."))

    @commands.command(name="ticketpanel")
    @commands.guild_only()
    async def ticketpanel_prefix_alias(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.author, discord.Member) or not ctx.guild:
            return
        if not await self._is_ticket_staff(ctx.author):
            return await ctx.send(
                embed=ModEmbed.error("Permission Denied", "You need ticket staff permissions for this action.")
            )
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send(embed=ModEmbed.error("Unavailable", "This command can only be used in text channels."))

        await self._send_ticket_panel_to_channel(ctx.channel, ctx.guild)
        await ctx.send(embed=ModEmbed.success("Panel Created", "Ticket panel has been created."))


async def setup(bot):
    await bot.add_cog(Tickets(bot))
