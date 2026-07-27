import asyncio
import logging
import discord
from discord.ext import commands
from pathlib import Path

from components_v2 import ensure_layout_view_action_rows

LOGGER = logging.getLogger("enzo-bot.custom-help")

HELP_BANNER_PATH = Path(__file__).resolve().parent / "assets" / "help_banner.png"
HELP_BANNER_FILENAME = "help-banner.png"

HELP_TOPICS: dict[str, dict[str, object]] = {
    "economy": {
        "title": "Economy Help",
        "summary": "Wallet, bank, income, trading, and item basics.",
        "commands": [
            "bal", "ecolb", "daily", "work", "beg", "fish", "hunt", "mine", "dailyspin",
            "lottery", "scratch", "invest", "investment", "stocks", "deposit", "withdraw",
            "transfer", "trade", "cd", "tools", "gearshop", "utilityshop", "buypotion",
            "usepotion", "effects", "resetshop", "resetcooldown", "prestige", "perks",
        ],
    },
    "casino": {
        "title": "Casino Help",
        "summary": "Solo and multiplayer gambling commands with bet limits.",
        "commands": [
            "casino", "gamble", "slots", "coinflip", "dice", "multidice", "colorduel",
            "uno", "blackjack", "roulette", "mines", "poker", "aln",
        ],
    },
    "pet": {
        "title": "Pet Help",
        "summary": "Buy, collect, evolve, fuse, and trade pets and pet materials.",
        "commands": [
            "pet", "pet shop", "pet collect", "pet info", "pet feed", "pet play",
            "pet evolution", "pet evolve", "pet cores", "pet fuse", "pet leaderboard",
            "petshop", "buypet", "mypets", "perks",
        ],
    },
    "crime": {
        "title": "Crime & Heist Help",
        "summary": "Risk commands, robberies, wanted status, and heist lobbies.",
        "commands": [
            "rob", "crime", "blackmarket", "tools", "jewelryheist", "bankrob", "teamrob",
        ],
    },
    "shop": {
        "title": "Shop Help",
        "summary": "Role shop, boost perks, prestige, and purchasable progression.",
        "commands": [
            "shop", "buy", "gearshop", "buygear", "utilityshop", "buyutility",
            "buypotion", "usepotion", "resetshop", "resetcooldown", "blackmarket",
            "booster", "prestige", "perks", "petshop", "buypet",
        ],
    },
    "level": {
        "title": "Level Help",
        "summary": "Level cards, XP leaderboards, and level reward roles.",
        "commands": [
            "profile", "profile edit", "profile save", "profile load", "profile delete",
            "pedit", "mslb", "rewards",
        ],
    },
    "fun": {
        "title": "Fun Help",
        "summary": "Games, jokes, rates, text filters, reactions, and social commands.",
        "commands": [
            "stats", "truth", "dare", "tod", "nhie", "wyr", "paranoia", "8ball", "fortune",
            "advice", "fact", "joke", "riddle", "quote", "hug", "slap", "pat",
            "roast", "compliment",
        ],
    },
    "nsfw": {
        "title": "NSFW Help",
        "summary": "Explicit commands for servers that have enabled NSFW features.",
        "commands": [
            "diddle", "spank", "tease", "striptease", "afterdark",
        ],
    },
    "admin": {
        "title": "Admin Help",
        "summary": "Complete economy control, setup, moderation-adjacent, and server config commands.",
        "commands": [
            "ecoadmin", "ecoadmin user", "ecoadmin bal", "ecoadmin add", "ecoadmin remove",
            "ecoadmin set", "ecoadmin bank", "ecoadmin bankadd", "ecoadmin bankremove",
            "ecoadmin reset", "ecoadmin wipe", "ecoadmin items", "ecoadmin itemgrant",
            "ecoadmin itemset", "ecoadmin itemreset", "ecoadmin pets", "ecoadmin petgrant",
            "ecoadmin petremove", "ecoadmin petitem", "ecoadmin petitemset", "ecoadmin petstage",
            "ecoadmin pethappy", "ecoadmin petrename", "ecoadmin petreset", "ecoadmin investments",
            "ecoadmin settle", "ecoadmin cancelinvest", "ecoadmin inv", "ecoadmin restock",
            "ecoadmin stock", "ecoadmin bminv", "ecoadmin bmrestock", "ecoadmin bmstock",
            "ecoadmin bmdiscount", "ecoadmin prestige", "ecoadmin booster", "ecoadmin roleperk",
            "ecoadmin rolereset", "ecoadmin wanted", "ecoadmin jail", "ecoadmin unjail",
            "ecoadmin lock", "ecoadmin cooldowns", "ecoadmin resetcd", "ecoadmin setcd",
            "ecoadmin games", "ecoadmin gamesclear", "ecoadmin config", "ecoadmin config channel",
            "ecoadmin config bypass", "ecoadmin history", "resetcd", "shopedit", "booster",
            "the-big-reset",
        ],
    },
}

TOPIC_ALIASES = {
    "money": "economy",
    "wallet": "economy",
    "bank": "economy",
    "gambling": "casino",
    "games": "casino",
    "game": "casino",
    "pets": "pet",
    "animal": "pet",
    "animals": "pet",
    "robbery": "crime",
    "heists": "crime",
    "store": "shop",
    "roles": "shop",
    "xp": "level",
    "rank": "level",
    "levels": "level",
    "adult": "nsfw",
    "explicit": "nsfw",
    "staff": "admin",
    "setup": "admin",
    "eco": "admin",
    "ecoadmin": "admin",
    "economyadmin": "admin",
}

HELP_FIELD_NAMES = (
    "Popular Commands",
    "More To Try",
    "Advanced Tools",
    "Extra Utilities",
    "Specialized Commands",
)

FEMBOY_HELP_FIELD_NAMES = (
    "Popuwaw Commandsies~ ✨",
    "Mowe To Twy Out, Cutie~ ♡",
    "Advanced Toowsies ✨",
    "Extwa Utiwities 🎀",
    "Speciawized Commandsies 💖",
)

class HelpTopicSelect(discord.ui.Select):
    def __init__(self, help_command: "CustomHelpCommand"):
        self.help_command = help_command
        options = [
            discord.SelectOption(
                label=help_command.decorate_title(
                    str(data["title"]).replace(" Help", "")
                ),
                value=topic,
                description=help_command.decorate_summary(str(data["summary"]))[:100],
            )
            for topic, data in HELP_TOPICS.items()
        ]
        super().__init__(
            placeholder=(
                "Pick a hewp categowy, cutie pie~ UwU 🎀"
                if help_command.femboy_mode
                else "Choose a help category..."
            ),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        topic = self.values[0]
        view = self.view
        if isinstance(view, HelpTopicView):
            await interaction.response.defer()
            await view.render_topic(topic)
            await view.update_public_message(interaction)
            return
        views = await self.help_command.build_topic_views(topic)
        from components_v2 import ensure_layout_view_action_rows
        await interaction.response.send_message(view=ensure_layout_view_action_rows(views[0]), ephemeral=True)

class HelpTopicPageButton(discord.ui.Button):
    def __init__(self, direction: int, *, disabled: bool):
        super().__init__(
            label="Previous" if direction < 0 else "Next",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, HelpTopicView) or view.selected_topic is None:
            return
        await interaction.response.defer()
        await view.render_topic(
            view.selected_topic, page=view.topic_page + self.direction
        )
        await view.update_public_message(interaction)


class HelpTopicView(discord.ui.LayoutView):
    def __init__(self, help_command: "CustomHelpCommand"):
        super().__init__(timeout=180)
        self.help_command = help_command
        self.selected_topic: str | None = None
        self.topic_page = 0
        self.render_home()

    def _select(self) -> HelpTopicSelect:
        select = HelpTopicSelect(self.help_command)
        if self.selected_topic is not None:
            title = str(HELP_TOPICS[self.selected_topic]["title"])
            select.placeholder = (
                f"Showin' chu {title}, UwU~ 💖"
                if self.help_command.femboy_mode
                else f"Showing {title}"
            )
        return select

    def _replace_container(self, children: list[discord.ui.Item]) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(*children, accent_color=0x5865F2))
        ensure_layout_view_action_rows(self)

    def render_home(self) -> None:
        self.selected_topic = None
        self.topic_page = 0
        children: list[discord.ui.Item] = []
        if HELP_BANNER_PATH.exists():
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{HELP_BANNER_FILENAME}")
                )
            )
            children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        children.extend(
            [
                discord.ui.TextDisplay(self.help_command.home_greeting()),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.ActionRow(self._select()),
            ]
        )
        self._replace_container(children)

    async def update_public_message(self, interaction: discord.Interaction) -> None:
        try:
            kwargs = dict(
                content=None,
                embeds=[],
                attachments=[],
                view=ensure_layout_view_action_rows(self),
            )
            if interaction.response.is_done():
                await interaction.edit_original_response(**kwargs)
            else:
                await interaction.response.edit_message(**kwargs)
        except discord.HTTPException:
            LOGGER.exception("Failed to update Help topic %s", self.selected_topic)
            await interaction.followup.send(
                "I could not update that help topic. Please try again.",
                ephemeral=True,
            )

    async def render_topic(self, topic: str, page: int | None = None) -> None:
        if topic != self.selected_topic:
            self.topic_page = 0
        if page is not None:
            self.topic_page = max(0, page)
        self.selected_topic = topic
        data = HELP_TOPICS[topic]
        commands_to_show = await self.help_command.filtered_commands(data["commands"])
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"**{self.help_command.decorate_title(str(data['title']))}**\n"
                f"{self.help_command.decorate_summary(str(data['summary']))}\n"
                f"{self.help_command.usage_hint()}"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        if commands_to_show:
            lines = [self.help_command.command_line(command) for command in commands_to_show]
            chunks = self.help_command.chunk_lines(lines, 2500)
            self.topic_page = min(self.topic_page, len(chunks) - 1)
            chunk = chunks[self.topic_page]
            children.append(
                discord.ui.TextDisplay(
                    f"**{self.help_command.field_name(self.topic_page + 1)}**\n{chunk}"
                )
            )
            if len(chunks) > 1:
                children.append(
                    discord.ui.ActionRow(
                        HelpTopicPageButton(
                            -1, disabled=self.topic_page == 0
                        ),
                        HelpTopicPageButton(
                            1, disabled=self.topic_page >= len(chunks) - 1
                        ),
                    )
                )
        else:
            children.append(
                discord.ui.TextDisplay(
                    f"**{self.help_command.empty_title()}**\n"
                    f"{self.help_command.empty_message()}"
                )
            )
        children.extend(
            [
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.ActionRow(self._select()),
            ]
        )
        self._replace_container(children)

class CustomHelpCommand(commands.HelpCommand):
    def __init__(self):
        super().__init__()
        self.accent_color = 0x5865F2
        self.femboy_mode = True

    def decorate_title(self, title: str) -> str:
        return f"🎀 {title} 🎀 UwU~" if self.femboy_mode else title

    def decorate_summary(self, summary: str) -> str:
        return (
            f"{summary} I weawwy hope dis hewps u, cutie pie!! 🥺💕"
            if self.femboy_mode
            else summary
        )

    def home_greeting(self) -> str:
        if self.femboy_mode:
            return (
                "**Hiiiii~ Hewwo!! I'm Mahito! UwU 🎀**\n"
                "How awe chu doin' today? Wha can I hewp my favowite cutie wiff? 🥺💕"
            )
        return "**Hello!**\nWhat do you need help with?"

    def usage_hint(self) -> str:
        if self.femboy_mode:
            return (
                f"Use `{self.prefix}help <command>` and I'ww show chu aww da "
                "weawwy cute detaiws!! 🥺✨"
            )
        return f"Use `{self.prefix}help <command>` for exact usage and aliases."

    def empty_title(self) -> str:
        return "Awww, Nuffin' Hewe~ 🥺💔" if self.femboy_mode else "Nothing Visible"

    def empty_message(self) -> str:
        if self.femboy_mode:
            return "I couwdn't find any commandsies hewe, I'm so sowwy cutie!! 🥺💖"
        return "No visible commands are available for you in this topic."

    @property
    def prefix(self) -> str:
        clean_prefix = getattr(self.context, "clean_prefix", None)
        if clean_prefix:
            return clean_prefix
        return "."

    def clean_name(self, value: str) -> str:
        return " ".join((value or "").lower().strip().split())

    def resolve_topic(self, value: str) -> str | None:
        key = self.clean_name(value)
        return key if key in HELP_TOPICS else TOPIC_ALIASES.get(key)

    def find_command(self, qualified_name: str) -> commands.Command | None:
        bot = getattr(self.context, "bot", None)
        if bot is None:
            return None
        parts = self.clean_name(qualified_name).split()
        if not parts:
            return None
        command = bot.get_command(parts[0])
        for part in parts[1:]:
            if command is None or not isinstance(command, commands.Group):
                return None
            command = command.get_command(part)
        return command

    def get_command_signature(self, command: commands.Command) -> str:
        signature = command.usage if command.usage is not None else command.signature
        base = f"{self.prefix}{command.qualified_name}"
        return f"{base} {signature}".strip()

    def get_command_description(self, command: commands.Command) -> str:
        return command.short_doc or command.help or "No description provided."

    def command_line(self, command: commands.Command) -> str:
        suffix = " 🎀" if self.femboy_mode else ""
        return f"`{self.get_command_signature(command)}` - {self.get_command_description(command)}{suffix}"

    def chunk_lines(self, lines: list[str], limit: int = 980) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current and current_len + line_len > limit:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def field_name(self, index: int, fallback: str = "Commands") -> str:
        names = FEMBOY_HELP_FIELD_NAMES if self.femboy_mode else HELP_FIELD_NAMES
        if index - 1 < len(names):
            name = names[index - 1]
        else:
            name = (
                f"Mowe {fallback}, UwU~ 💕"
                if self.femboy_mode
                else f"{fallback} Continued"
            )
        return name

    async def send_views(self, views: list[discord.ui.LayoutView]) -> None:
        from components_v2 import ensure_layout_view_action_rows
        destination = self.get_destination()
        for view in views:
            await destination.send(view=ensure_layout_view_action_rows(view))

    async def filtered_commands(
        self, command_names: list[str]
    ) -> list[commands.Command]:
        commands_to_show: list[commands.Command] = []
        seen: set[str] = set()
        for command_name in command_names:
            command = self.find_command(command_name)
            if command is None or command.qualified_name in seen:
                continue
            commands_to_show.append(command)
            seen.add(command.qualified_name)
        return await self.filter_commands(commands_to_show, sort=False)

    async def send_bot_help(self, mapping) -> None:
        kwargs = {"view": HelpTopicView(self)}
        if HELP_BANNER_PATH.exists():
            kwargs["file"] = discord.File(HELP_BANNER_PATH, filename=HELP_BANNER_FILENAME)
        await self.get_destination().send(**kwargs)

    async def build_topic_views(self, topic: str) -> list[discord.ui.LayoutView]:
        from components_v2 import branded_notice_view
        data = HELP_TOPICS[topic]
        commands_to_show = await self.filtered_commands(data["commands"])
        
        base_desc = (
            f"**{self.decorate_title(str(data['title']))}**\n"
            f"{self.decorate_summary(str(data['summary']))}\n"
            f"{self.usage_hint()}\n\n"
        )
        
        if not commands_to_show:
            view = branded_notice_view(
                title="",
                description=base_desc + f"**{self.empty_title()}**\n{self.empty_message()}",
                accent_color=self.accent_color,
            )
            return [view]

        lines = [self.command_line(command) for command in commands_to_show]
        views = []
        
        current_desc = base_desc
        fields_count = 0
        
        for index, chunk in enumerate(self.chunk_lines(lines), start=1):
            addition = f"**{self.field_name(index)}**\n{chunk}\n\n"
            if fields_count >= 25 or len(current_desc) + len(addition) > 3800:
                views.append(branded_notice_view(title="", description=current_desc.strip(), accent_color=self.accent_color))
                current_desc = f"**{self.decorate_title(str(data['title']))}**\n{self.decorate_summary(str(data['summary']))}\n\n"
                fields_count = 0
            current_desc += addition
            fields_count += 1
            
        if current_desc.strip():
            views.append(branded_notice_view(title="", description=current_desc.strip(), accent_color=self.accent_color))
            
        return views

    async def send_topic_help(self, topic: str) -> None:
        views = await self.build_topic_views(topic)
        await self.send_views(views)

    async def send_cog_help(self, cog) -> None:
        from components_v2 import branded_notice_view
        commands_to_show = await self.filter_commands(cog.get_commands(), sort=True)
        
        base_desc = (
            f"**{self.decorate_title(f'{cog.qualified_name} Commands')}**\n"
            f"{self.decorate_summary(cog.description or 'Commands in this module.')}\n\n"
        )
        
        lines = [self.command_line(command) for command in commands_to_show]
        if not lines:
            view = branded_notice_view(
                title="",
                description=base_desc + f"**{self.empty_title()}**\n{self.empty_message()}",
                accent_color=self.accent_color,
            )
            await self.send_views([view])
            return
            
        views = []
        current_desc = base_desc
        fields_count = 0
        
        for index, chunk in enumerate(self.chunk_lines(lines), start=1):
            addition = f"**{self.field_name(index)}**\n{chunk}\n\n"
            if fields_count >= 25 or len(current_desc) + len(addition) > 3800:
                views.append(branded_notice_view(title="", description=current_desc.strip(), accent_color=self.accent_color))
                current_desc = f"**{self.decorate_title(f'{cog.qualified_name} Commands')}**\n\n"
                fields_count = 0
            current_desc += addition
            fields_count += 1
            
        if current_desc.strip():
            views.append(branded_notice_view(title="", description=current_desc.strip(), accent_color=self.accent_color))
            
        await self.send_views(views)

    async def send_group_help(self, group: commands.Group) -> None:
        await self.send_command_help(group)

    async def send_command_help(self, command: commands.Command) -> None:
        from components_v2 import branded_notice_view, ensure_layout_view_action_rows
        
        description = (
            f"**{self.decorate_title(f'{self.prefix}{command.qualified_name}')}**\n"
            f"{self.decorate_summary(command.help or command.short_doc or 'No description provided.')}\n\n"
            f"**{'How To Use It, Cutie~ 🎀' if self.femboy_mode else 'Usage'}**\n"
            f"`{self.get_command_signature(command)}`"
        )
        
        if command.aliases:
            description += f"\n\n**{'Cute Awiases~ 💖' if self.femboy_mode else 'Aliases'}**\n"
            description += ", ".join(f"`{alias}`" for alias in command.aliases)
            
        if isinstance(command, commands.Group):
            subcommands = await self.filter_commands(command.commands, sort=True)
            if subcommands:
                lines = [self.command_line(subcommand) for subcommand in subcommands]
                for index, chunk in enumerate(self.chunk_lines(lines), start=1):
                    header = (
                        ("Wittwe Subcommandsies~ 🥺" if index == 1 else "Mowe Wittwe Subcommandsies~ ✨")
                        if self.femboy_mode
                        else ("Subcommands" if index == 1 else "More Subcommands")
                    )
                    description += f"\n\n**{header}**\n{chunk}"
                    
        topic_names = [
            topic
            for topic, data in HELP_TOPICS.items()
            if command.qualified_name in data["commands"]
        ]
        if topic_names:
            footer = ("Mowe cute hewpies: " if self.femboy_mode else "Related topic: ") + ", ".join(f"{self.prefix}help {topic}" for topic in topic_names)
            description += f"\n\n_{footer}_"

        view = branded_notice_view(
            title="",
            description=description.strip(),
            accent_color=self.accent_color,
        )
        await self.get_destination().send(view=ensure_layout_view_action_rows(view))

    async def send_error_message(self, error: str) -> None:
        from components_v2 import branded_notice_view, ensure_layout_view_action_rows
        
        title = "Awww, I Couwdn't Find Dat~ 🥺💔" if self.femboy_mode else "Help Not Found"
        description = (
            f"{error}\n\n"
            + (
                f"Pwease twy `{self.prefix}help` and I'ww show chu aww my wittwe hewp topics, cutie!! UwU 🎀"
                if self.femboy_mode
                else f"Use `{self.prefix}help` to see topics or `{self.prefix}help casino` for games."
            )
        )
        view = branded_notice_view(
            title=title,
            description=description,
            accent_color=0xED4245,
        )
        await self.get_destination().send(view=ensure_layout_view_action_rows(view))

    async def command_callback(self, ctx, *, command: str | None = None) -> None:
        self.context = ctx
        self.femboy_mode = True
        if ctx.guild is not None:
            settings = await asyncio.to_thread(
                ctx.bot.guild_settings.get_settings, ctx.guild.id
            )
            self.femboy_mode = settings.get("femboy_mode") is not False
        if command is None:
            return await self.send_bot_help(self.get_bot_mapping())

        requested = self.clean_name(command)
        topic = self.resolve_topic(requested)
        if topic is not None and " " not in requested:
            return await self.send_topic_help(topic)

        found = self.find_command(requested)
        if found is None:
            return await self.send_error_message(
                f"No command or help topic named `{command}` exists."
            )
        return await self.send_command_help(found)
