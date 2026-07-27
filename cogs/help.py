"""
Advanced Help System
Features:
- Interactive /help with categories, search, and detailed info
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, List, Dict, Any
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import Config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _normalize_command_name(value: str) -> str:
    value = (value or "").strip()
    if value.startswith(("/", ",")):
        value = value[1:]
    return " ".join(value.lower().split())


def _walk_slash_commands(tree: app_commands.CommandTree) -> list[app_commands.Command | app_commands.Group]:
    cmds: list[app_commands.Command | app_commands.Group] = []
    for cmd in tree.walk_commands():
        if isinstance(cmd, app_commands.ContextMenu):
            continue
        if isinstance(cmd, (app_commands.Command, app_commands.Group)):
            cmds.append(cmd)
    return cmds


def _category_for_command(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        binding = getattr(cmd, "binding", None)
        cog_name = getattr(binding, "__cog_name__", None)
    else:
        cog_name = cmd.cog_name

    if cog_name:
        if cog_name.upper() == cog_name and len(cog_name) <= 4:
            return cog_name
        if cog_name == "AIModeration":
            return "AI Moderation"
        return cog_name
    return "Core"


def _format_invocation(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        return f"/{cmd.qualified_name}"
    return f",{cmd.qualified_name}"


def _chunked(items: list, *, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _usage_line(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    parts = [_format_invocation(cmd)]
    
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        for p in getattr(cmd, "parameters", []):
            if p.required:
                parts.append(f"<{p.name}>")
            else:
                parts.append(f"[{p.name}]")
    else:
        # Prefix command
        for name, param in cmd.clean_params.items():
            if param.default is param.empty:
                parts.append(f"<{name}>")
            else:
                parts.append(f"[{name}]")
                
    return " ".join(parts)


def _parameter_lines(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    lines: list[str] = []
    
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        params = list(getattr(cmd, "parameters", []))
        if not params:
            return "No parameters."
        for p in params:
            desc = (p.description or "No description").strip()
            required = "required" if p.required else "optional"
            lines.append(f"• `{p.name}` ({required}) — {desc}")
    else:
        # Prefix command
        if not cmd.clean_params:
            return "No parameters."
        for name, param in cmd.clean_params.items():
            required = "required" if param.default is param.empty else "optional"
            lines.append(f"• `{name}` ({required})")
            
    return "\n".join(lines)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _command_summary(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    return _shorten(getattr(cmd, "description", None) or getattr(cmd, "help", None) or "No description.", 78)


def _command_type_label(cmd: app_commands.Command | app_commands.Group | commands.Command) -> str:
    if isinstance(cmd, app_commands.Group):
        return "Slash group"
    if isinstance(cmd, app_commands.Command):
        return "Slash command"
    if isinstance(cmd, commands.Group):
        return "Prefix group"
    return "Prefix command"


@dataclass(frozen=True)
class _HelpIndex:
    categories: dict[str, list[app_commands.Command | app_commands.Group | commands.Command]]
    by_name: dict[str, app_commands.Command | app_commands.Group | commands.Command]

    @staticmethod
    def build(
        bot: commands.Bot,
        *,
        include_slash: bool = True,
        include_prefix: bool = True,
    ) -> "_HelpIndex":
        categories: dict[str, list[app_commands.Command | app_commands.Group | commands.Command]] = {}
        by_name: dict[str, app_commands.Command | app_commands.Group | commands.Command] = {}

        if include_slash:
            for cmd in _walk_slash_commands(bot.tree):
                if getattr(cmd, "name", None) == "help":
                    continue

                category = _category_for_command(cmd)
                categories.setdefault(category, []).append(cmd)
                by_name.setdefault(_normalize_command_name(cmd.qualified_name), cmd)

        if include_prefix:
            seen_prefix: set[str] = set()
            for cmd in bot.walk_commands():
                if cmd.hidden:
                    continue

                qname = _normalize_command_name(cmd.qualified_name)
                if qname in seen_prefix:
                    continue
                seen_prefix.add(qname)

                category = _category_for_command(cmd)
                categories.setdefault(category, []).append(cmd)
                by_name.setdefault(qname, cmd)

                for alias in cmd.aliases:
                    by_name.setdefault(_normalize_command_name(alias), cmd)
                    if cmd.parent:
                        parent_name = _normalize_command_name(cmd.parent.qualified_name)
                        by_name.setdefault(_normalize_command_name(f"{parent_name} {alias}"), cmd)

        for cat in categories:
            categories[cat].sort(key=lambda c: c.qualified_name)

        return _HelpIndex(categories=categories, by_name=by_name)


# =============================================================================
# CATEGORY ICONS
# =============================================================================

CATEGORY_ICONS = {
    "Moderation": "Mod",
    "Admin": "Admin",
    "Roles": "Roles",
    "Voice": "Voice",
    "Tickets": "Tickets",
    "Staff": "Staff",
    "Court": "Court",
    "AutoMod": "AutoMod",
    "AI Moderation": "AI",
    "Utility": "Utility",
    "Fun": "Fun",
    "Core": "Core",
    "Whitelist": "Whitelist",
}


def get_category_icon(category: str) -> str:
    return CATEGORY_ICONS.get(category, "Other")

# =============================================================================
# HELP VIEW
# =============================================================================

class HelpView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        author_id: int,
        index: _HelpIndex,
        mode: str = "slash",
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.author_id = author_id
        self.index = index
        self.mode = mode
        self.message: Optional[discord.Message] = None

        self.category: str = "Overview"
        self.pages: list[discord.Embed] = [self._build_overview_embed()]
        self.page_idx: int = 0

        self._select = discord.ui.Select(
            placeholder="Choose a help section...",
            options=self._build_category_options(),
            min_values=1,
            max_values=1,
            row=0,
        )
        self._select.callback = self._on_category_selected
        self.add_item(self._select)

        self._refresh_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help menu belongs to someone else. Use /help or ,help to open your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    def _build_category_options(self) -> list[discord.SelectOption]:
        opts: list[discord.SelectOption] = [
            discord.SelectOption(
                label="Start Here",
                value="Overview",
                description="Best commands, workflows, and how help works",
            ),
            discord.SelectOption(
                label="All Commands",
                value="__all__",
                description="Complete slash and prefix command index",
            ),
            discord.SelectOption(
                label="Search & Examples",
                value="__search__",
                description="How to find a command quickly",
            ),
        ]
        for category in sorted(self.index.categories.keys()):
            count = len(self.index.categories[category])
            label = f"{get_category_icon(category)} ({count})"
            opts.append(
                discord.SelectOption(
                    label=label[:100],
                    value=category,
                    description=f"Browse {count} {category} command{'s' if count != 1 else ''}",
                )
            )
        return opts[:25]

    def _help_label(self) -> str:
        return "/help or ,help"

    def _details_hint(self) -> str:
        return "Use /help command:<name> or ,help <name>"

    def _build_overview_embed(self) -> discord.Embed:
        total = sum(len(v) for v in self.index.categories.values())
        slash_count = sum(
            1
            for commands_list in self.index.categories.values()
            for cmd in commands_list
            if isinstance(cmd, (app_commands.Command, app_commands.Group))
        )
        prefix_count = total - slash_count
        top_categories = sorted(
            self.index.categories.items(),
            key=lambda item: (-len(item[1]), item[0].lower()),
        )[:8]

        embed = discord.Embed(
            title="Ass Moderation Help",
            description=(
                "Use `/help` or `,help` to browse the same command index. "
                "Pick a category below, or search a command directly with "
                "`/help command:<name>` / `,help <name>`."
            ),
            color=Config.COLOR_EMBED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Fast Start",
            value=(
                "`/setup` - Build the basic server roles/channels\n"
                "`/settings` - Open server configuration\n"
                "`/automod status` - Review filter health\n"
                "`/automod help` - Configure content filters\n"
                "`/aimod status` - Check AI moderation\n"
                "`/ticket create` or `,ticket create` - Open support"
            ),
            inline=False,
        )
        embed.add_field(
            name="Common Moderator Flow",
            value=(
                "`/warn user:<member> reason:<reason>`\n"
                "`/timeout user:<member> duration:30m reason:<reason>`\n"
                "`/history user:<member>`\n"
                "`/purge amount:25`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Natural AI Actions",
            value=(
                "Mention the bot with a clear action:\n"
                "`@Ass Moderation warn @user spamming`\n"
                "`@Ass Moderation lock this channel`\n"
                "`@Ass Moderation summarize recent activity`"
            ),
            inline=True,
        )
        if top_categories:
            embed.add_field(
                name="Largest Categories",
                value="\n".join(
                    f"`{category}` - {len(commands_list)} commands"
                    for category, commands_list in top_categories
                ),
                inline=False,
            )
        embed.add_field(
            name="Index Size",
            value=f"{total} commands: {slash_count} slash, {prefix_count} prefix.",
            inline=False,
        )
        embed.set_footer(text="Dropdown: browse categories | Buttons: page through results")
        return embed

    def _build_command_list_pages(
        self,
        *,
        title: str,
        commands_list: list[app_commands.Command | app_commands.Group | commands.Command]
    ) -> list[discord.Embed]:
        lines: list[str] = []
        for cmd in commands_list:
            invocation = _format_invocation(cmd)
            kind = "slash" if isinstance(cmd, (app_commands.Command, app_commands.Group)) else "prefix"
            lines.append(f"`{invocation}` [{kind}] - {_command_summary(cmd)}")

        pages: list[discord.Embed] = []
        chunks = list(_chunked(lines, size=9)) or [[]]
        for idx, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title=title,
                description="\n".join(chunk) if chunk else "No commands found.",
                color=Config.COLOR_EMBED,
            )
            embed.add_field(
                name="Need details?",
                value="Use `/help command:<name>` or `,help <name>`.",
                inline=False,
            )
            embed.set_footer(text=f"Page {idx}/{len(chunks)}")
            pages.append(embed)
        return pages
    def _build_search_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Search & Examples",
            description="The help system accepts slash names, prefix names, aliases, and subcommands.",
            color=Config.COLOR_INFO,
        )
        embed.add_field(
            name="Direct Lookup",
            value=(
                "`/help command:ban`\n"
                "`,help ban`\n"
                "`/help command:automod setup`\n"
                "`,help ticket close`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Browse",
            value=(
                "Use the dropdown for categories.\n"
                "Choose **All Commands** for the full index.\n"
                "Use buttons to move between pages."
            ),
            inline=True,
        )
        embed.add_field(
            name="Useful Starting Points",
            value=(
                "`/automod help` - filter setup\n"
                "`/aimod status` - AI moderation state\n"
                "`/settings` - server systems\n"
                "`/help command:warn` - command details\n"
                "`/setup` - missing roles/channels"
            ),
            inline=False,
        )
        embed.set_footer(text="Tip: command lookup is fuzzy, so partial names work too.")
        return embed

    async def _on_category_selected(self, interaction: discord.Interaction) -> None:
        value = self._select.values[0]
        self.page_idx = 0

        if value == "Overview":
            self.category = "Overview"
            self.pages = [self._build_overview_embed()]
        elif value == "__all__":
            self.category = "All Commands"
            all_cmds: list[app_commands.Command | app_commands.Group | commands.Command] = []
            for cat in sorted(self.index.categories.keys()):
                all_cmds.extend(self.index.categories[cat])
            all_cmds.sort(key=lambda c: c.qualified_name)
            self.pages = self._build_command_list_pages(title="All Commands", commands_list=all_cmds)
        elif value == "__search__":
            self.category = "Search"
            self.pages = [self._build_search_embed()]
        else:
            self.category = value
            emoji = get_category_icon(value)
            cmds = self.index.categories.get(value, [])
            self.pages = self._build_command_list_pages(title=f"{emoji} {value}", commands_list=cmds)

        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    def _refresh_buttons(self) -> None:
        last = len(self.pages) - 1
        self.first_button.disabled = self.page_idx <= 0
        self.prev_button.disabled = self.page_idx <= 0
        self.next_button.disabled = self.page_idx >= last
        self.last_button.disabled = self.page_idx >= last
        self.page_counter.label = f"{self.page_idx + 1}/{len(self.pages)}"

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary, row=1)
    async def first_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_idx = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.primary, row=1)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_idx = max(0, self.page_idx - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def page_counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, row=1)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_idx = min(len(self.pages) - 1, self.page_idx + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary, row=1)
    async def last_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_idx = len(self.pages) - 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_idx], view=self)


# =============================================================================
# HELP COG
# =============================================================================

class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _build_details_embed(
        self,
        cmd: app_commands.Command | app_commands.Group | commands.Command,
    ) -> discord.Embed:
        category = _category_for_command(cmd)
        title = f"Help: {_format_invocation(cmd)}"
        desc = _command_summary(cmd)
        is_slash = isinstance(cmd, (app_commands.Command, app_commands.Group))

        embed = discord.Embed(
            title=title,
            description=desc,
            color=Config.COLOR_EMBED,
        )
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Type", value=_command_type_label(cmd), inline=True)
        embed.add_field(name="Run it", value=f"`{_usage_line(cmd)}`", inline=False)
        embed.add_field(name="Inputs", value=_parameter_lines(cmd), inline=False)

        if isinstance(cmd, commands.Command) and cmd.aliases:
            aliases = ", ".join(f"`{alias}`" for alias in cmd.aliases[:15])
            embed.add_field(name="Aliases", value=aliases, inline=False)

        if isinstance(cmd, app_commands.Group):
            subcommands = sorted(child.qualified_name for child in cmd.commands)
            if subcommands:
                lines = [f"`/{name}`" for name in subcommands[:12]]
                if len(subcommands) > 12:
                    lines.append(f"... and {len(subcommands) - 12} more")
                embed.add_field(name="Subcommands", value="\n".join(lines), inline=False)
        elif isinstance(cmd, commands.Group):
            subcommands = sorted(child.qualified_name for child in cmd.commands)
            if subcommands:
                lines = [f"`,{name}`" for name in subcommands[:12]]
                if len(subcommands) > 12:
                    lines.append(f"... and {len(subcommands) - 12} more")
                embed.add_field(name="Subcommands", value="\n".join(lines), inline=False)

        examples: list[str] = []
        if cmd.qualified_name.startswith("ticket"):
            examples = [
                "`/ticket create`" if is_slash else "`,ticket create general Need help`",
                "`/ticket close reason:resolved`" if is_slash else "`,ticket close resolved`",
                "`/ticket transcript`" if is_slash else "`,ticket transcript`",
            ]
        elif cmd.qualified_name.startswith("automod"):
            examples = ["`/automod help`", "`/automod status`", "`/automod setup`"]
        elif cmd.qualified_name.startswith("aimod"):
            examples = ["`/aimod status`", "`/aimod configure enabled:True talking:True`"]
        elif is_slash:
            examples.append(f"`/{cmd.qualified_name}`")
            if isinstance(cmd, app_commands.Command):
                first_required = next((p for p in cmd.parameters if p.required), None)
                if first_required:
                    examples.append(f"`/{cmd.qualified_name} {first_required.name}:<value>`")
        else:
            examples.append(f"`,{cmd.qualified_name}`")
            if cmd.clean_params:
                parts = []
                for name, param in cmd.clean_params.items():
                    parts.append(f"<{name}>" if param.default is param.empty else f"[{name}]")
                examples.append(f"`,{cmd.qualified_name} {' '.join(parts)}`")

        if examples:
            embed.add_field(name="Examples", value="\n".join(examples), inline=False)

        embed.set_footer(text="Same lookup works through /help and ,help")
        return embed

    def _build_unified_index(self) -> _HelpIndex:
        return _HelpIndex.build(self.bot, include_slash=True, include_prefix=True)

    def _find_command(self, index: _HelpIndex, command: str):
        key = _normalize_command_name(command)
        cmd = index.by_name.get(key)
        if cmd:
            return cmd
        matches = [name for name in index.by_name.keys() if key and key in name]
        if matches:
            return index.by_name[matches[0]]
        return None

    def _not_found_text(self, index: _HelpIndex, command: str, help_label: str) -> str:
        key = _normalize_command_name(command)
        matches = [name for name in index.by_name.keys() if key and key in name]
        if matches:
            suggestions = ", ".join([f"`{m}`" for m in matches[:5]])
            return f"I could not find `{command}` exactly. Closest matches: {suggestions}."
        return f"I could not find `{command}`. Try `{help_label}` to browse categories or search a shorter name."

    @commands.command(name="help", help="Browse commands and get detailed help")
    async def help_prefix(self, ctx: commands.Context, *, command: Optional[str] = None):
        """Browse the same help index used by /help."""
        index = self._build_unified_index()

        if command:
            cmd = self._find_command(index, command)
            if not cmd:
                await ctx.send(self._not_found_text(index, command, ",help"), delete_after=15)
                return
            await ctx.send(embed=self._build_details_embed(cmd))
            return

        view = HelpView(bot=self.bot, author_id=ctx.author.id, index=index, mode="unified")
        view.message = await ctx.send(embed=view.pages[0], view=view)

    @app_commands.command(name="help", description="Browse commands and get detailed help")
    @app_commands.describe(command="Specific command to view (example: ban, warn, vc)")
    async def help_slash(self, interaction: discord.Interaction, command: Optional[str] = None) -> None:
        index = self._build_unified_index()

        if command:
            cmd = self._find_command(index, command)
            if not cmd:
                await interaction.response.send_message(
                    self._not_found_text(index, command, "/help"),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(embed=self._build_details_embed(cmd), ephemeral=True)
            return

        view = HelpView(bot=self.bot, author_id=interaction.user.id, index=index, mode="unified")
        await interaction.response.send_message(embed=view.pages[0], view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @help_slash.autocomplete("command")
    async def help_autocomplete(self, interaction: discord.Interaction, current: str):
        index = self._build_unified_index()
        q = _normalize_command_name(current)

        results: list[app_commands.Choice[str]] = []
        for name, cmd in sorted(index.by_name.items(), key=lambda kv: kv[0]):
            if q and q not in name:
                continue
            label = _format_invocation(cmd)
            results.append(app_commands.Choice(name=label, value=cmd.qualified_name))
            if len(results) >= 25:
                break
        return results

async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
