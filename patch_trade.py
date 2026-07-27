import codecs
with codecs.open('c:/Users/Dell/Soul/guild/economy_system.py', 'r', 'utf-8') as f:
    code = f.read()

# 1. Update TradeView base class to LayoutView
code = code.replace('class TradeView(discord.ui.View):', 'class TradeView(discord.ui.LayoutView):')

# 2. Add guild_id to __init__
init_target = '''        super().__init__(timeout=300)
        self.cog = cog
        self.state = TradeState(guild_id, user_a.id, user_b.id)'''

init_replace = '''        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.state = TradeState(guild_id, user_a.id, user_b.id)'''
code = code.replace(init_target, init_replace)

# 3. Update _format_items to pass guild
format_items_target = '''    def _format_items(self, user_id: int) -> str:
        side = self.state._side(user_id)
        lines = []
        if side["credits"] > 0:
            lines.append(f"*   **{side['credits']:,}** credits")
        for pet_id, pet_name in side["pets"]:
            lines.append(f"*   {get_valk_emoji('pet')} **{pet_name}** (#{pet_id})")'''

format_items_replace = '''    def _format_items(self, user_id: int) -> str:
        side = self.state._side(user_id)
        guild = self.cog.bot.get_guild(self.guild_id)
        lines = []
        if side["credits"] > 0:
            lines.append(f"*   **{side['credits']:,}** credits")
        for pet_id, pet_name in side["pets"]:
            lines.append(f"*   {get_valk_emoji(guild, 'pet')} **{pet_name}** (#{pet_id})")'''
code = code.replace(format_items_target, format_items_replace)

# 4. Update _remove_options to pass guild
remove_options_target = '''    def _remove_options(self, user_id: int) -> list[discord.SelectOption]:
        side = self.state._side(user_id)
        options = []
        if side["credits"] > 0:
            options.append(
                discord.SelectOption(
                    label=f"{side['credits']:,} credits",
                    value="credits:credits",
                    emoji="💰",
                )
            )
        for pet_id, pet_name in side["pets"]:
            options.append(
                discord.SelectOption(
                    label=pet_name[:100],
                    value=f"pet:{pet_id}",
                    emoji=get_valk_emoji("pet"),
                )
            )'''

remove_options_replace = '''    def _remove_options(self, user_id: int) -> list[discord.SelectOption]:
        side = self.state._side(user_id)
        guild = self.cog.bot.get_guild(self.guild_id)
        options = []
        if side["credits"] > 0:
            options.append(
                discord.SelectOption(
                    label=f"{side['credits']:,} credits",
                    value="credits:credits",
                    emoji="💰",
                )
            )
        for pet_id, pet_name in side["pets"]:
            options.append(
                discord.SelectOption(
                    label=pet_name[:100],
                    value=f"pet:{pet_id}",
                    emoji=get_valk_emoji(guild, "pet"),
                )
            )'''
code = code.replace(remove_options_target, remove_options_replace)

with codecs.open('c:/Users/Dell/Soul/guild/economy_system.py', 'w', 'utf-8') as f:
    f.write(code)
