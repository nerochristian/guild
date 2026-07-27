import discord
from discord.ext import commands
import random as random_module
import secrets
import time

random = secrets.SystemRandom()
import aiohttp
import asyncio
from v2_embed import apply_v2_embed_layout
from fun_content import FUN_CONTENT


GIF_REQUEST_HEADERS = {
    "User-Agent": "MahitoBot/1.0 (+https://github.com/nerochristian/guild)"
}
GIF_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def send_v2(ctx, embed, **kwargs):
    view = discord.ui.LayoutView()

    files = kwargs.get("files")
    if not files and "file" in kwargs:
        files = [kwargs["file"]]

    apply_v2_embed_layout(view, embed=embed, files=files)
    return await ctx.send(view=view, **kwargs)


async def edit_v2(message, embed, **kwargs):
    view = discord.ui.LayoutView()

    files = kwargs.get("files")
    if not files and "file" in kwargs:
        files = [kwargs["file"]]

    apply_v2_embed_layout(view, embed=embed, files=files)
    return await message.edit(view=view, **kwargs)


async def is_nsfw_enabled(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return False
    settings = await asyncio.to_thread(ctx.bot.guild_settings.get_settings, ctx.guild.id)
    return bool(settings.get("nsfw_enabled"))


def _is_blacklist_administrator(ctx: commands.Context) -> bool:
    allowed_user_ids = getattr(ctx.bot, "allowed_user_ids", frozenset())
    if ctx.author.id in allowed_user_ids:
        return True

    owner_id = getattr(ctx.bot, "owner_id", None)
    if owner_id is not None and ctx.author.id == owner_id:
        return True

    permissions = getattr(ctx.author, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def _is_protected_blacklist_target(
    ctx: commands.Context, target: discord.User
) -> bool:
    if target.id == ctx.bot.user.id or target.id in getattr(
        ctx.bot, "allowed_user_ids", frozenset()
    ):
        return True

    owner_id = getattr(ctx.bot, "owner_id", None)
    if owner_id is not None and target.id == owner_id:
        return True

    if ctx.guild is None:
        return False
    if target.id == ctx.guild.owner_id:
        return True

    member = ctx.guild.get_member(target.id)
    return bool(member and member.guild_permissions.administrator)

class FunCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.eightball_responses = FUN_CONTENT["eightball"]
        self.color = 0x2B2D31
        self.started_at = time.monotonic()
        self.last_spank_gif = None
        self.last_tease_gif = None
        self.last_striptease_gif = None

    def create_embed(self, title, description, color=None):
        return discord.Embed(
            title=title, description=description, color=color or self.color
        )

    def _member_roll(
        self, member: discord.Member, offset: int, low: int, high: int
    ) -> int:
        return random_module.Random(member.id + offset).randint(low, high)

    @commands.command(help="Show bot runtime and Discord statistics")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def stats(self, ctx: commands.Context):
        total_seconds = max(0, int(time.monotonic() - self.started_at))
        days, remainder = divmod(total_seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, seconds = divmod(remainder, 60)

        uptime_parts = []
        if days:
            uptime_parts.append(f"{days}d")
        if hours or days:
            uptime_parts.append(f"{hours}h")
        if minutes or hours or days:
            uptime_parts.append(f"{minutes}m")
        uptime_parts.append(f"{seconds}s")

        channel_count = sum(len(guild.channels) for guild in self.bot.guilds)
        ping_ms = max(0, round(self.bot.latency * 1_000))
        description = "\n".join(
            (
                f"**Uptime:** `{' '.join(uptime_parts)}`",
                f"**Ping:** `{ping_ms} ms`",
                f"**Servers:** `{len(self.bot.guilds):,}`",
                f"**Users:** `{len(self.bot.users):,}`",
                f"**Channels:** `{channel_count:,}`",
                f"**Commands:** `{len(self.bot.commands):,}`",
            )
        )
        embed = self.create_embed("Bot Stats", description, color=0x5865F2)
        if self.bot.user is not None:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await send_v2(ctx, embed=embed)

    # Truth or Dare
    @commands.command(help="Ask a truth question")
    async def truth(self, ctx: commands.Context):
        truths = [
            "What's the most embarrassing thing you've ever done?",
            "What's a secret you've never told anyone?",
            "Who is your crush?",
            "What's the worst lie you've ever told?",
            "If you could switch lives with anyone for a day, who would it be?",
            "What's your biggest fear?",
            "What's the weirdest dream you've ever had?",
            "Have you ever stolen anything?",
        ]
        embed = self.create_embed(
            "🤔 Truth", f"**{random.choice(FUN_CONTENT['truths'])}**", color=0x5865F2
        )
        embed.set_author(
            name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Give a dare")
    async def dare(self, ctx: commands.Context):
        dares = [
            "Send a voice message of you singing a song.",
            "Post a selfie right now.",
            "Change your nickname to something I choose for 24 hours.",
            "Let me write your next status.",
            "Do 10 pushups right now.",
            "Speak in a different accent for the next 10 minutes.",
            "Text your crush and say 'I like you'.",
            "Eat a spoonful of hot sauce.",
        ]
        embed = self.create_embed(
            "😈 Dare", f"**{random.choice(FUN_CONTENT['dares'])}**", color=0xED4245
        )
        embed.set_author(
            name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Truth or Dare")
    async def tod(self, ctx: commands.Context):
        if random.choice([True, False]):
            await self.truth(ctx)
        else:
            await self.dare(ctx)

    # Never Have I Ever
    @commands.command(help="Never Have I Ever")
    async def nhie(self, ctx: commands.Context):
        statements = [
            "Never have I ever been arrested.",
            "Never have I ever cheated on a test.",
            "Never have I ever ghosted someone.",
            "Never have I ever lied about my age.",
            "Never have I ever fallen asleep in class/work.",
            "Never have I ever met a celebrity.",
            "Never have I ever broken a bone.",
            "Never have I ever eaten food off the floor.",
        ]
        embed = self.create_embed(
            "🙅 Never Have I Ever",
            f"**{random.choice(FUN_CONTENT['nhie'])}**",
            color=0xFEE75C,
        )
        await send_v2(ctx, embed=embed)

    # Would You Rather
    @commands.command(help="Would You Rather")
    async def wyr(self, ctx: commands.Context):
        questions = [
            "Would you rather be able to fly or be invisible?",
            "Would you rather have unlimited money or unlimited time?",
            "Would you rather live in the past or the future?",
            "Would you rather be always cold or always hot?",
            "Would you rather lose your sight or your hearing?",
            "Would you rather speak all languages or be able to speak to animals?",
            "Would you rather never have to sleep or never have to eat?",
            "Would you rather live without music or live without television/movies?",
        ]
        embed = self.create_embed(
            "⚖️ Would You Rather",
            f"**{random.choice(FUN_CONTENT['wyr'])}**",
            color=0x9B59B6,
        )
        embed.set_footer(text="React below with your choice!")
        msg = await ctx.send(embed=embed)
        await msg.add_reaction("1️⃣")
        await msg.add_reaction("2️⃣")

    # Paranoia
    @commands.command(help="Play a game of Paranoia")
    async def paranoia(self, ctx: commands.Context):
        questions = [
            "Who is most likely to become a millionaire?",
            "Who has the best fashion sense?",
            "Who is most likely to survive a zombie apocalypse?",
            "Who is the most secretly evil?",
            "Who would be the first to die in a horror movie?",
            "Who gives the best advice?",
            "Who is most likely to get arrested for something stupid?",
            "Who is the biggest flirt?",
        ]
        embed = self.create_embed(
            "👁️ Paranoia",
            f"**{random.choice(FUN_CONTENT['paranoia'])}**\n*(Answer with a user mention!)*",
            color=0x34495E,
        )
        await send_v2(ctx, embed=embed)

    # Fortune Telling
    @commands.command(name="8ball", help="Ask the Magic 8-Ball a question")
    async def eightball(self, ctx: commands.Context, *, question: str = ""):
        if not question:
            embed = self.create_embed(
                "🎱 Magic 8-Ball", "Please ask a question!", color=0xED4245
            )
            await send_v2(ctx, embed=embed)
            return

        embed = self.create_embed(
            "🎱 Magic 8-Ball",
            f"**Question:** {question}\n\n**Answer:** {random.choice(self.eightball_responses)}",
            color=0x000000,
        )
        embed.set_author(
            name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Open a fortune cookie")
    async def fortune(self, ctx: commands.Context):
        fortunes = [
            "A beautiful, smart, and loving person will be coming into your life.",
            "A fresh start will put you on your way.",
            "All your hard work will soon pay off.",
            "Allow compassion to guide your decisions.",
            "An agreeable romance might begin to take on the appearance.",
            "Believe in yourself and others will too.",
            "Disaster is near. Watch your back.",
            "Good news will come to you by mail.",
            "You will find true love today.",
        ]
        embed = self.create_embed(
            "🥠 Fortune Cookie",
            f"*{random.choice(FUN_CONTENT['fortunes'])}*",
            color=0xF1C40F,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Get some random advice")
    async def advice(self, ctx: commands.Context):
        embed = self.create_embed(
            "💡 Daily Advice",
            f"*{random.choice(FUN_CONTENT['advice'])}*",
            color=0x1ABC9C,
        )
        await send_v2(ctx, embed=embed)

    # Random info generators
    @commands.command(help="Learn a random fact")
    async def fact(self, ctx: commands.Context):
        embed = self.create_embed(
            "🧠 Random Fact",
            f"**{random.choice(FUN_CONTENT['facts'])}**",
            color=0x3498DB,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Tell a random joke")
    async def joke(self, ctx: commands.Context):
        embed = self.create_embed(
            "🤣 Joke", f"**{random.choice(FUN_CONTENT['jokes'])}**", color=0xE67E22
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Tell a dad joke")
    async def dadjoke(self, ctx: commands.Context):
        embed = self.create_embed(
            "👨 Dad Joke",
            f"*{random.choice(FUN_CONTENT['dadjokes'])}*",
            color=0x95A5A6,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Solve a riddle")
    async def riddle(self, ctx: commands.Context):
        r = random.choice(FUN_CONTENT["riddles"])
        embed = self.create_embed(
            "🤔 Riddle Time",
            f"**{r['q']}**\n\n**Answer:** ||{r['a']}||",
            color=0x8E44AD,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Get a random quote")
    async def quote(self, ctx: commands.Context):
        quote = random.choice(FUN_CONTENT["quotes"])
        embed = self.create_embed("📜 Quote", f'"{quote}"', color=0x2ECC71)
        await send_v2(ctx, embed=embed)

    @commands.command(help="Check your daily horoscope")
    async def horoscope(self, ctx: commands.Context, sign: str = ""):
        signs = [
            "aries",
            "taurus",
            "gemini",
            "cancer",
            "leo",
            "virgo",
            "libra",
            "scorpio",
            "sagittarius",
            "capricorn",
            "aquarius",
            "pisces",
        ]
        if sign.lower() not in signs:
            embed = self.create_embed(
                "🔮 Horoscope",
                f"Please provide a valid zodiac sign!\n`{', '.join(signs)}`",
                color=0xED4245,
            )
            await send_v2(ctx, embed=embed)
            return

        embed = self.create_embed(
            f"🔮 {sign.capitalize()} Horoscope",
            f"*{random.choice(FUN_CONTENT['horoscopes'])}*",
            color=0x9B59B6,
        )
        embed.set_author(
            name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
        )
        await send_v2(ctx, embed=embed)

    # Social/Rating
    @commands.command(help="Check the romantic compatibility of two users")
    async def ship(
        self, ctx: commands.Context, user1: discord.Member, user2: discord.Member = None
    ):
        if user2 is None:
            user2 = ctx.author

        rating = (user1.id + user2.id) % 101
        bar = "█" * (rating // 10) + "░" * (10 - (rating // 10))

        msg = f"🔻 **{user1.display_name}**\n🔺 **{user2.display_name}**\n\n**Match Score:** {rating}%\n`[{bar}]`"

        if rating > 80:
            msg += "\n*Perfect match! 💞*"
        elif rating < 20:
            msg += "\n*Oof... 💔*"

        embed = self.create_embed("💖 Matchmaking", msg, color=0xFF69B4)
        await send_v2(ctx, embed=embed)

    @commands.command(help="Measure someone's IQ")
    async def iq(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        iq = self._member_roll(member, 10, 10, 200)

        embed = self.create_embed(
            "🧠 IQ Test", f"**{member.display_name}**'s IQ is **{iq}**!", color=0x3498DB
        )
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
        await send_v2(ctx, embed=embed)

    @commands.command(help="Measure someone's... size")
    async def pp(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        length = self._member_roll(member, 0, 1, 15)
        shaft = "=" * length

        embed = self.create_embed(
            "🍆 PP Size",
            f"**{member.display_name}**'s pp:\n`8{shaft}D`",
            color=0x5865F2,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Check how much of a simp someone is")
    async def simp(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        percent = self._member_roll(member, 1, 0, 100)

        embed = self.create_embed(
            "🥺 Simp Meter",
            f"**{member.display_name}** is **{percent}%** simp!",
            color=0xFFB6C1,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Check how sus someone is")
    async def sus(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        percent = self._member_roll(member, 2, 0, 100)

        embed = self.create_embed(
            "ඞ Sus Meter",
            f"**{member.display_name}** is **{percent}%** sus!",
            color=0xED4245,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Check someone's gay meter")
    async def gay(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        percent = self._member_roll(member, 3, 0, 100)

        embed = self.create_embed(
            "🏳️‍🌈 Gay Meter",
            f"**{member.display_name}** is **{percent}%** gay!",
            color=0x1ABC9C,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(
        help="Check someone's femboy meter, or let an admin toggle femboy help mode.",
        usage="[@member|on|off]",
    )
    @commands.guild_only()
    async def femboy(self, ctx: commands.Context, *, target: str = None):
        normalized = (target or "").lower().strip()
        if normalized in {"on", "off"}:
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("Only administrators can change femboy help mode.")
                return

            enabled = normalized == "on"
            await asyncio.to_thread(
                ctx.bot.guild_settings.save_settings,
                ctx.guild.id,
                femboy_mode=enabled,
            )
            if enabled:
                await ctx.send("Femboy help mode is **on** now, uwu ♡")
            else:
                await ctx.send("Femboy help mode is **off** now.")
            return

        member = ctx.author
        if target:
            try:
                member = await commands.MemberConverter().convert(ctx, target)
            except commands.MemberNotFound:
                await ctx.send("I couldn't find that member.")
                return
        percent = self._member_roll(member, 5, 0, 100)

        embed = self.create_embed(
            "👗 Femboy Meter",
            f"**{member.display_name}** is **{percent}%** femboy!",
            color=0xFFB6C1,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Ratio a message")
    async def ratio(self, ctx: commands.Context):
        if not ctx.message.reference:
            embed = self.create_embed(
                "⚖️ Ratio",
                "You need to reply to a message to ratio them!",
                color=0xED4245,
            )
            await send_v2(ctx, embed=embed)
            return

        try:
            msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
            embed = self.create_embed(
                "⚖️ Ratio", "The ratio has begun...", color=0x5865F2
            )
            await send_v2(ctx, embed=embed)
        except Exception:
            embed = self.create_embed("⚖️ Ratio", "Failed to ratio.", color=0xED4245)
            await send_v2(ctx, embed=embed)

    @commands.command(help="Rate someone's hotness")
    async def hotornot(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        rating = self._member_roll(member, 4, 1, 10)

        embed = self.create_embed(
            "🔥 Hot or Not",
            f"**{member.display_name}** is a **{rating}/10**!",
            color=0xE67E22,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Rate anything out of 10")
    async def rate(self, ctx: commands.Context, *, thing: str = ""):
        if not thing:
            embed = self.create_embed(
                "⭐ Rate", "What do you want me to rate?", color=0xED4245
            )
            await send_v2(ctx, embed=embed)
            return
        rating = random.randint(0, 10)
        embed = self.create_embed(
            "⭐ Rate", f"I rate **{thing}** a **{rating}/10**!", color=0xF1C40F
        )
        await send_v2(ctx, embed=embed)

    # Fun Text Modifiers
    def text_embed(self, ctx, title, text):
        embed = self.create_embed(title, text)
        embed.set_author(
            name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
        )
        return embed

    @commands.command(help="Convert text into emojis")
    async def emojify(self, ctx: commands.Context, *, text: str):
        emojified = ""
        for char in text.lower():
            if char.isalpha():
                emojified += f":regional_indicator_{char}: "
            elif char.isdigit():
                numbers = [
                    "zero",
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                ]
                emojified += f":{numbers[int(char)]}: "
            elif char == " ":
                emojified += "   "
            elif char == "?":
                emojified += ":grey_question: "
            elif char == "!":
                emojified += ":grey_exclamation: "
            else:
                emojified += char
        await send_v2(ctx, embed=self.text_embed(ctx, "🔡 Emojify", emojified))

    @commands.command(help="Convert text into morse code")
    async def morse(self, ctx: commands.Context, *, text: str):
        morse_dict = {
            "a": ".-",
            "b": "-...",
            "c": "-.-.",
            "d": "-..",
            "e": ".",
            "f": "..-.",
            "g": "--.",
            "h": "....",
            "i": "..",
            "j": ".---",
            "k": "-.-",
            "l": ".-..",
            "m": "--",
            "n": "-.",
            "o": "---",
            "p": ".--.",
            "q": "--.-",
            "r": ".-.",
            "s": "...",
            "t": "-",
            "u": "..-",
            "v": "...-",
            "w": ".--",
            "x": "-..-",
            "y": "-.--",
            "z": "--..",
            "0": "-----",
            "1": ".----",
            "2": "..---",
            "3": "...--",
            "4": "....-",
            "5": ".....",
            "6": "-....",
            "7": "--...",
            "8": "---..",
            "9": "----.",
            " ": " / ",
        }
        res = " ".join([morse_dict.get(c, c) for c in text.lower()])
        await send_v2(ctx, embed=self.text_embed(ctx, "📻 Morse Code", f"`{res}`"))

    @commands.command(help="Convert text into binary")
    async def binary(self, ctx: commands.Context, *, text: str):
        res = " ".join([format(ord(c), "08b") for c in text])
        await send_v2(ctx, embed=self.text_embed(ctx, "💻 Binary", f"`{res}`"))

    @commands.command(help="UwUify your text")
    async def uwu(self, ctx: commands.Context, *, text: str):
        text = (
            text.replace("r", "w").replace("l", "w").replace("R", "W").replace("L", "W")
        )
        text = text.replace("no", "nyo").replace("na", "nya")
        faces = ["(・`ω´・)", ";;w;;", "owo", "UwU", ">w<", "^w^"]
        await send_v2(
            ctx, embed=self.text_embed(ctx, "💕 UwU", f"{text} {random.choice(faces)}")
        )

    @commands.command(help="Add claps between words")
    async def clap(self, ctx: commands.Context, *, text: str):
        res = " 👏 ".join(text.split())
        await send_v2(ctx, embed=self.text_embed(ctx, "👏 Clap", f"👏 {res} 👏"))

    @commands.command(help="A E S T H E T I C")
    async def vaporwave(self, ctx: commands.Context, *, text: str):
        res = "".join([chr(ord(c) + 65248) if 33 <= ord(c) <= 126 else c for c in text])
        await send_v2(ctx, embed=self.text_embed(ctx, "🌌 Vaporwave", res))

    @commands.command(help="Corrupt your text")
    async def zalgo(self, ctx: commands.Context, *, text: str):
        marks = list(map(chr, range(768, 879)))
        res = ""
        for c in text:
            res += c
            for _ in range(random.randint(1, 3)):
                res += random.choice(marks)
        await send_v2(ctx, embed=self.text_embed(ctx, "👁️ Zalgo", res))

    # Helper to fetch anime gifs
    async def fetch_gif(self, category: str):
        if not category:
            return None

        # Hardcoded 100% SFW anime spank gifs from Tenor (curated)
        spank_gifs = [
            "https://media.tenor.com/iz6t2EwKeYMAAAAC/rikka-takanashi-chunibyo.gif",
            "https://media.tenor.com/Sp7yE5UzqFMAAAAC/spank-slap.gif",
            "https://media.tenor.com/uER90n0laEEAAAAC/anime-spanking.gif",
            "https://media.tenor.com/Tj6GzyCetQwAAAAC/spank-rank.gif",
            "https://media.tenor.com/l_-8WyYoZ0YAAAAC/anime-spank-akebi-chan.gif",
            "https://media.tenor.com/c-kO_ArEbn4AAAAC/inukami-anime.gif",
            "https://media.tenor.com/7BG6hzxWKBgAAAAC/anime-anime86.gif",
            "https://media.tenor.com/-jzXG7HfVNkAAAAC/demon-slayer-tengen-uzui.gif",
            "https://media.tenor.com/XDi4rIQ9Sm8AAAAC/lol-funny.gif",
            "https://media.tenor.com/qhdaIdgiV78AAAAC/anime-spank.gif",
            "https://media.tenor.com/kuEoHmR2JegAAAAC/enig674.gif",
            "https://media.tenor.com/B5oC9lACJ9kAAAAC/anime-spank.gif",
            "https://media.tenor.com/bHE5Txlp5-8AAAAC/slap-butts-anime.gif",
            "https://media.tenor.com/negrhG1qdE0AAAAC/utena-x-gamatoto.gif",
            "https://media.tenor.com/LMKZH2bDKHsAAAAC/spank-anime.gif",
            "https://media.tenor.com/zlM4Im2DJO4AAAAC/spank-spanked.gif",
            "https://media.tenor.com/eyIeo_JX_akAAAAC/anime-spanking.gif",
            "https://media.tenor.com/-_xQj5Hy8U0AAAAC/sailor-moon-chibi-moon.gif",
            "https://media.tenor.com/VlSXTbFcvDQAAAAC/naruto-anime.gif",
            "https://media.tenor.com/dLTy1MX-mhUAAAAC/sena-soruko-ceo-whip-nev.gif",
            "https://media.tenor.com/bDFN3ejJEL8AAAAC/butt-slap-hunter-x-hunter.gif",
            "https://media.tenor.com/vzQLL0MsF0cAAAAC/darkelfcarla-windmill.gif",
            "https://media.tenor.com/H1IZv4_EGXEAAAAC/shinobu-kocho-giyu-tomioka.gif",
            "https://media.tenor.com/ykCjxWVvq18AAAAC/slap-slapping.gif"
        ]

        if category == "spank":
            import random
            available = [g for g in spank_gifs if g != self.last_spank_gif]
            if not available:
                available = spank_gifs
            chosen = random.choice(available)
            self.last_spank_gif = chosen
            return chosen

        # Hardcoded 100% SFW anime tease gifs from Tenor (curated)
        tease_gifs = [
            "https://media.tenor.com/db96_0PFMpcAAAAC/ano-natsu-ano-natsu-de-matteru.gif",
            "https://media.tenor.com/X51YpCwWbBIAAAAC/konata-brain-less.gif",
            "https://media.tenor.com/Az0peT6Wu8kAAAAC/sweetness-and-lightning-smirk.gif",
            "https://media.tenor.com/hm5x7U1aUpAAAAAC/tomodachi-no-imouto-ga-ore-ni-dake-uzai-my-friend%27s-little-sister-has-it-in-for-me.gif",
            "https://media.tenor.com/ZglUG-3bnOAAAAAC/anime-shikimoris-not-just-cute.gif",
            "https://media.tenor.com/t0t6VhgbXm0AAAAC/bleh-blehh.gif",
            "https://media.tenor.com/na75CxaFzN4AAAAC/anime-grin.gif",
            "https://media.tenor.com/FbrgN1UUM4EAAAAC/tomodachi-no-imouto-ga-ore-ni-dake-uzai-my-friend%27s-little-sister-has-it-in-for-me.gif",
            "https://media.tenor.com/BOrIBe7S5_MAAAAC/nagatoro-noodletoro.gif",
            "https://media.tenor.com/miRFMPcWbx8AAAAC/nono-anime.gif",
            "https://media.tenor.com/6w0LdfZ67O0AAAAC/anime-smirk.gif",
            "https://media.tenor.com/vPx3z-62dA8AAAAC/nagatoro-hayase-nagatoro.gif",
            "https://media.tenor.com/SE9ud-M5RsYAAAAC/kushima-kamome.gif",
            "https://media.tenor.com/YAojDiX4H5EAAAAC/anime.gif",
            "https://media.tenor.com/ra3oGQqu5DUAAAAC/kisumi-free.gif",
            "https://media.tenor.com/NyMbJzK77tsAAAAC/dont-toy-with-me-miss-nagatoro-dont-bully-me-miss-nagatoro.gif",
            "https://media.tenor.com/jN4GoorI3pwAAAAC/lenny-face.gif",
            "https://media.tenor.com/0cbDJtdfo60AAAAC/shikimori-shikimoris-not-just-cute.gif",
            "https://media.tenor.com/6zhhQPo3JyYAAAAC/anime-anime-girl.gif",
            "https://media.tenor.com/Yo5ipzk1DIoAAAAC/isekai-wa-smartphone-to-tomo-ni-in-another-world-with-my-smartphone.gif",
            "https://media.tenor.com/zWvk98sKXmEAAAAC/tease-taunt.gif",
            "https://media.tenor.com/7P2NiwpYJlMAAAAC/anime-shikimoris-not-just-cute.gif",
            "https://media.tenor.com/s73hsxgU3IAAAAAC/suou-yuki-roshidere.gif",
            "https://media.tenor.com/xq2d1Oj0OwUAAAAC/marin-kitagawa.gif",
            "https://media.tenor.com/EHIkianDEOEAAAAC/hayase-nagatoro-smug-nagatoro-smug.gif",
            "https://media.tenor.com/jy5jmRyL_dsAAAAC/anime-medaka-box.gif",
            "https://media.tenor.com/Ky_lCk6n8_IAAAAC/nagatoro-ijiranaide-nagatoro-san.gif",
            "https://media.tenor.com/JRVW8ZhsmSYAAAAC/boy-girl.gif",
            "https://media.tenor.com/AoHOV8GAcfsAAAAC/anime-medaka-box.gif"
        ]

        if category == "tease":
            import random
            available = [g for g in tease_gifs if g != self.last_tease_gif]
            if not available:
                available = tease_gifs
            chosen = random.choice(available)
            self.last_tease_gif = chosen
            return chosen

        # Hardcoded 100% SFW anime striptease gifs from Tenor (curated)
        striptease_gifs = [
            "https://media.tenor.com/gngkjtXxAQoAAAAC/bocchi-the-rock-hiroi-kikuri.gif",
            "https://media.tenor.com/V1jlRUYPEqcAAAAC/misty-stripping.gif",
            "https://media.tenor.com/LQKn1UEUWHYAAAAC/achannel-ichii-tooru.gif",
            "https://media.tenor.com/zhObc7TC6DUAAAAC/servamp-lily.gif",
            "https://media.tenor.com/oIhcyizZtpwAAAAC/anime-fujimiya.gif",
            "https://media.tenor.com/Iw4Dv8jwgPsAAAAC/made-in-abyss-riko.gif",
            "https://media.tenor.com/9k0zsvR09_cAAAAC/england-hetalia.gif",
            "https://media.tenor.com/CfUbjkOc40AAAAAC/kou-ichijo-take-off-shirt.gif",
            "https://media.tenor.com/o2-_5lT33PAAAAAC/demon-slayer-demon-slayer-kanjiro.gif",
            "https://media.tenor.com/tm58UhDByCIAAAAC/jaeha-yona.gif",
            "https://media.tenor.com/LWktd3exfRAAAAAC/excel-excel-saga.gif",
            "https://media.tenor.com/ZACjfWbGi2YAAAAC/satanichia-undress.gif",
            "https://media.tenor.com/1O_cReNda0IAAAAC/onsen-hot-springs.gif",
            "https://media.tenor.com/Na_PkecwEBEAAAAC/skate-leading-stars-itsuki-kiriyama.gif",
            "https://media.tenor.com/6qAaiz0q7PkAAAAC/uryu-undress.gif",
            "https://media.tenor.com/5X3WVvX6MlkAAAAC/strip-muscles.gif",
            "https://media.tenor.com/vygA9X4GZssAAAAC/sulogna-sanyal.gif",
            "https://media.tenor.com/NZPt0JGD4X4AAAAC/nagisa-hazuki-anime.gif",
            "https://media.tenor.com/sQxouiO5UDAAAAAC/undressing-undress.gif",
            "https://media.tenor.com/66nlMejQ0hsAAAAC/chainsaw-man-csm.gif",
            "https://media.tenor.com/syIHT2QeRwMAAAAC/dynazenon-chise-asukagawa.gif",
            "https://media.tenor.com/J7ruNE3O6VkAAAAC/marin-kitagawa-kitagawa-marin.gif"
        ]

        if category == "striptease":
            import random
            available = [g for g in striptease_gifs if g != self.last_striptease_gif]
            if not available:
                available = striptease_gifs
            chosen = random.choice(available)
            self.last_striptease_gif = chosen
            return chosen

        # Giphy configuration for other SFW roleplay commands
        giphy_api_key = "eyR0Fpo6ZloUaw7aRXjyNYf4gOZPOjDh"
        giphy_map = {
            "diddle": "anime tickle",
            "afterdark": "anime kiss"
        }

        async with aiohttp.ClientSession(
            headers=GIF_REQUEST_HEADERS,
            timeout=GIF_REQUEST_TIMEOUT,
        ) as session:
            try:
                if category in giphy_map:
                    query = giphy_map[category].replace(" ", "+")
                    url = f"https://api.giphy.com/v1/gifs/search?api_key={giphy_api_key}&q={query}&limit=25"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results = data.get("data", [])
                            if results:
                                return random.choice(results)["images"]["original"]["url"]

                # Mapping custom categories to nekos.best endpoints
                endpoint_map = {
                    "nom": "feed",
                    "bonk": "slap",
                    "kick": "yeet",
                    "lick": "kiss",
                    "handhold": "cuddle",
                    "glomp": "hug",
                    "kill": "shoot",
                    "spank": "slap",
                    "tease": "smug",
                    "striptease": "dance",
                    "afterdark": "kiss",
                }
                mapped_category = endpoint_map.get(category, category)

                # Fallback to nekos.best
                async with session.get(
                    f"https://nekos.best/api/v2/{mapped_category}"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["results"][0]["url"]
            except Exception:
                return None
        return None

    async def fetch_safebooru(self, tags: str, blocked_tags: tuple[str, ...] = ()):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&tags={tags}+rating:safe"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data:
                            random.shuffle(data)
                            for item in data:
                                item_tags = str(item.get("tags", "")).lower()
                                if any(tag.lower() in item_tags for tag in blocked_tags):
                                    continue
                                return f"https://safebooru.org/images/{item['directory']}/{item['image']}"
        except Exception as e:
            print(f"Safebooru fetch failed: {e}")
        return None

    # Roleplay/Action Commands
    async def _rp_action(
        self,
        ctx: commands.Context,
        member: discord.Member,
        emoji: str,
        action: str,
        api_category: str,
        color=None,
    ):
        target = member.mention if member else "themselves"
        if member == ctx.author:
            target = "themselves"
        embed = discord.Embed(
            description=f"{emoji} {ctx.author.mention} **{action}** {target}!",
            color=color or self.color,
        )

        gif_url = await self.fetch_gif(api_category)
        if gif_url:
            embed.set_image(url=gif_url)

        await send_v2(ctx, embed=embed)

    @commands.command(help="Give someone a hug")
    async def hug(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🤗", "hugs", "hug", 0xFFB6C1)

    @commands.command(help="Slap someone")
    async def slap(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "👋", "slaps", "slap", 0xED4245)

    @commands.command(help="Pat someone on the head")
    async def pat(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "✋", "pats", "pat", 0x2ECC71)

    @commands.command(help="Kiss someone")
    async def kiss(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💋", "kisses", "kiss", 0xE91E63)

    @commands.command(help="Diddle someone (NSFW)")
    @commands.check(is_nsfw_enabled)
    async def diddle(self, ctx: commands.Context, member: discord.Member = None):
        target = member.mention if member else "themselves"
        if member == ctx.author:
            target = "themselves"
        embed = discord.Embed(
            description=f"🍆 {ctx.author.mention} diddles {target}!",
            color=0x8B0000,
        )
        embed.set_image(
            url="https://media.tenor.com/g3byvxzYHL4AAAAC/this-is-my-kingdom-cum-cum.gif"
        )

        await send_v2(ctx, embed=embed)

    async def _nsfw_action(
        self,
        ctx: commands.Context,
        member: discord.Member | None,
        action: str,
        api_category: str,
    ) -> None:
        target = member.mention if member and member != ctx.author else "themselves"
        if "{target}" in action:
            desc = f"{ctx.author.mention} " + action.format(target=target)
        else:
            desc = f"{ctx.author.mention} {action} {target}."
            
        embed = discord.Embed(
            description=desc,
            color=0x8B0000,
        )
        gif_url = await self.fetch_gif(api_category)
        if gif_url:
            embed.set_image(url=gif_url)
        await send_v2(ctx, embed=embed)

    @commands.command(help="Give someone a hard spank (NSFW)")
    @commands.check(is_nsfw_enabled)
    async def spank(self, ctx: commands.Context, member: discord.Member = None):
        await self._nsfw_action(ctx, member, "spanks the hell out of {target} 🍑", "spank")

    @commands.command(help="Tease someone mercilessly (NSFW)")
    @commands.check(is_nsfw_enabled)
    async def tease(self, ctx: commands.Context, member: discord.Member = None):
        await self._nsfw_action(ctx, member, "mercilessly teases {target} 😈", "tease")

    @commands.command(name="striptease", aliases=["strip"], help="Perform a seductive striptease (NSFW)")
    @commands.check(is_nsfw_enabled)
    async def striptease(self, ctx: commands.Context, member: discord.Member = None):
        await self._nsfw_action(
            ctx, member, "performs a seductive striptease for {target} ✨", "striptease"
        )

    @commands.command(help="Take someone to the bedroom (NSFW)")
    @commands.check(is_nsfw_enabled)
    async def afterdark(self, ctx: commands.Context, member: discord.Member = None):
        await self._nsfw_action(
            ctx, member, "drags {target} into the bedroom... 🛏️", "afterdark"
        )

    @commands.command(help="Poke someone")
    async def poke(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "👉", "pokes", "poke", 0x3498DB)

    @commands.command(help="Cuddle with someone")
    async def cuddle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🥰", "cuddles with", "cuddle", 0xFF69B4)

    @commands.command(help="Bite someone")
    async def bite(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🦷", "bites", "bite", 0x992D22)

    @commands.command(help="Tickle someone")
    async def tickle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🪶", "tickles", "tickle", 0xF1C40F)

    @commands.command(help="Give someone a headpat")
    async def headpat(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💆", "gives a headpat to", "pat", 0x1ABC9C)

    @commands.command(help="Bonk someone")
    async def bonk(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🏏", "bonks", "bonk", 0x95A5A6)

    @commands.command(help="Yeet someone")
    async def yeet(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "☄️", "yeets", "yeet", 0xE67E22)

    @commands.command(help="Feed someone")
    async def feed(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🍔", "feeds", "nom", 0xF39C12)

    @commands.command(help="Call someone a baka")
    async def baka(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💢", "calls baka on", "baka", 0xE74C3C)

    @commands.command(help="Kill someone")
    async def kill(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🗡️", "kills", "kill", 0x000000)

    @commands.command(
        aliases=["marriage", "marrage", "propose"], help="Propose to someone"
    )
    async def marry(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💍", "proposes to", "hug", 0xFFD700)

    @commands.command(help="Divorce someone")
    async def divorce(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💔", "divorces", "kick", 0x7F8C8D)

    @commands.command(help="Steal from someone")
    async def steal(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🦝", "steals from", "smug", 0x2C3E50)

    @commands.command(help="Lick someone")
    async def lick(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "👅", "licks", "lick", 0xE84393)

    @commands.command(help="Stare at someone")
    async def stare(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "👁️", "stares at", "stare", 0x34495E)

    @commands.command(help="Throw something at someone")
    async def throw(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(
            ctx, member, "⚾", "throws something at", "yeet", 0xBDC3C7
        )

    @commands.command(help="Carry someone")
    async def carry(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💪", "carries", "hug", 0xE67E22)

    @commands.command(help="High-five someone")
    async def highfive(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🙌", "high fives", "highfive", 0x2ECC71)

    @commands.command(help="Hold hands with someone")
    async def handhold(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(
            ctx, member, "🤝", "holds hands with", "handhold", 0xFFC312
        )

    @commands.command(help="Give someone a peck")
    async def peck(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "😗", "pecks", "kiss", 0xFDA7DF)

    @commands.command(help="Nuzzle someone")
    async def nuzzle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "😸", "nuzzles", "cuddle", 0xD980FA)

    @commands.command(help="Glomp someone")
    async def glomp(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🫂", "glomps", "glomp", 0x833471)

    @commands.command(help="Tackle someone")
    async def tackle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🏈", "tackles", "yeet", 0xC4E538)

    # Solo Actions
    async def _solo_action(
        self,
        ctx: commands.Context,
        member: discord.Member,
        emoji: str,
        action: str,
        target_action: str,
        api_category: str,
        color=None,
    ):
        if member and member != ctx.author:
            desc = f"{emoji} {ctx.author.mention} **{target_action}** {member.mention}!"
        else:
            desc = f"{emoji} {ctx.author.mention} **{action}**!"

        embed = discord.Embed(description=desc, color=color or self.color)

        gif_url = await self.fetch_gif(api_category)
        if gif_url:
            embed.set_image(url=gif_url)

        await send_v2(ctx, embed=embed)

    @commands.command(help="Cry")
    async def cry(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😭", "is crying", "cries on", "cry", 0x3498DB
        )

    @commands.command(help="Blush")
    async def blush(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😳", "is blushing", "blushes at", "blush", 0xE74C3C
        )

    @commands.command(help="Act like a neko")
    async def neko(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx,
            member,
            "🐱",
            "acts like a neko",
            "acts like a neko at",
            "neko",
            0xF1C40F,
        )

    @commands.command(help="Look smug")
    async def smug(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😏", "looks smug", "smirks at", "smug", 0x9B59B6
        )

    @commands.command(help="Dance")
    async def dance(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "💃", "is dancing", "dances with", "dance", 0xE84393
        )

    @commands.command(help="Laugh")
    async def laugh(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😂", "laughs", "laughs at", "laugh", 0xF39C12
        )

    @commands.command(help="Facepalm")
    async def facepalm(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🤦", "facepalms", "facepalms at", "facepalm", 0x95A5A6
        )

    @commands.command(help="Pout")
    async def pout(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😡", "pouts", "pouts at", "pout", 0xC0392B
        )

    @commands.command(help="Shrug")
    async def shrug(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🤷", "shrugs", "shrugs at", "shrug", 0x7F8C8D
        )

    @commands.command(help="Wave")
    async def wave(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "👋", "waves", "waves at", "wave", 0x3498DB
        )

    @commands.command(help="Wink")
    async def wink(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😉", "winks", "winks at", "wink", 0x1ABC9C
        )

    @commands.command(help="Yawn")
    async def yawn(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🥱", "yawns", "yawns at", "sleep", 0xBDC3C7
        )

    @commands.command(help="Be naughty")
    async def naughty(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx,
            member,
            "😈",
            "is being naughty",
            "acts naughty towards",
            "smug",
            0x8E44AD,
        )

    # Other Actions
    @commands.command(help="Roast someone")
    async def roast(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        embed = self.create_embed(
            "🔥 Roast",
            f"{member.mention}, {random.choice(FUN_CONTENT['roasts'])}",
            color=0xE74C3C,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Compliment someone")
    async def compliment(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        embed = self.create_embed(
            "💖 Compliment",
            f"{member.mention}, {random.choice(FUN_CONTENT['compliments'])}",
            color=0xFF69B4,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Fake hack someone")
    async def hack(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            embed = self.create_embed(
                "💻 Hack", "Who do you want to hack?", color=0xED4245
            )
            await send_v2(ctx, embed=embed)
            return

        embed = self.create_embed(
            "💻 Hack Sequence Initiated",
            f"Target: **{member.display_name}**...",
            color=0x2ECC71,
        )
        msg = await send_v2(ctx, embed=embed)

        await asyncio.sleep(1)
        embed.description = f"Finding IP address...\n`[192.168.{random.randint(0, 255)}.{random.randint(0, 255)}]`"
        await edit_v2(msg, embed=embed)

        await asyncio.sleep(1)
        embed.description += "\nBypassing firewall... `[SUCCESS]`"
        await edit_v2(msg, embed=embed)

        await asyncio.sleep(1)
    async def throw(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(
            ctx, member, "⚾", "throws something at", "yeet", 0xBDC3C7
        )

    @commands.command(help="Carry someone")
    async def carry(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "💪", "carries", "hug", 0xE67E22)

    @commands.command(help="High-five someone")
    async def highfive(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🙌", "high fives", "highfive", 0x2ECC71)

    @commands.command(help="Hold hands with someone")
    async def handhold(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(
            ctx, member, "🤝", "holds hands with", "handhold", 0xFFC312
        )

    @commands.command(help="Give someone a peck")
    async def peck(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "😗", "pecks", "kiss", 0xFDA7DF)

    @commands.command(help="Nuzzle someone")
    async def nuzzle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "😸", "nuzzles", "cuddle", 0xD980FA)

    @commands.command(help="Glomp someone")
    async def glomp(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🫂", "glomps", "glomp", 0x833471)

    @commands.command(help="Tackle someone")
    async def tackle(self, ctx: commands.Context, member: discord.Member = None):
        await self._rp_action(ctx, member, "🏈", "tackles", "yeet", 0xC4E538)

    # Solo Actions
    async def _solo_action(
        self,
        ctx: commands.Context,
        member: discord.Member,
        emoji: str,
        action: str,
        target_action: str,
        api_category: str,
        color=None,
    ):
        if member and member != ctx.author:
            desc = f"{emoji} {ctx.author.mention} **{target_action}** {member.mention}!"
        else:
            desc = f"{emoji} {ctx.author.mention} **{action}**!"

        embed = discord.Embed(description=desc, color=color or self.color)

        gif_url = await self.fetch_gif(api_category)
        if gif_url:
            embed.set_image(url=gif_url)

        await send_v2(ctx, embed=embed)

    @commands.command(help="Cry")
    async def cry(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😭", "is crying", "cries on", "cry", 0x3498DB
        )

    @commands.command(help="Blush")
    async def blush(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😳", "is blushing", "blushes at", "blush", 0xE74C3C
        )

    @commands.command(help="Act like a neko")
    async def neko(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx,
            member,
            "🐱",
            "acts like a neko",
            "acts like a neko at",
            "neko",
            0xF1C40F,
        )

    @commands.command(help="Look smug")
    async def smug(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😏", "looks smug", "smirks at", "smug", 0x9B59B6
        )

    @commands.command(help="Dance")
    async def dance(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "💃", "is dancing", "dances with", "dance", 0xE84393
        )

    @commands.command(help="Laugh")
    async def laugh(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😂", "laughs", "laughs at", "laugh", 0xF39C12
        )

    @commands.command(help="Facepalm")
    async def facepalm(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🤦", "facepalms", "facepalms at", "facepalm", 0x95A5A6
        )

    @commands.command(help="Pout")
    async def pout(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😡", "pouts", "pouts at", "pout", 0xC0392B
        )

    @commands.command(help="Shrug")
    async def shrug(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🤷", "shrugs", "shrugs at", "shrug", 0x7F8C8D
        )

    @commands.command(help="Wave")
    async def wave(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "👋", "waves", "waves at", "wave", 0x3498DB
        )

    @commands.command(help="Wink")
    async def wink(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "😉", "winks", "winks at", "wink", 0x1ABC9C
        )

    @commands.command(help="Yawn")
    async def yawn(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx, member, "🥱", "yawns", "yawns at", "sleep", 0xBDC3C7
        )

    @commands.command(help="Be naughty")
    async def naughty(self, ctx: commands.Context, member: discord.Member = None):
        await self._solo_action(
            ctx,
            member,
            "😈",
            "is being naughty",
            "acts naughty towards",
            "smug",
            0x8E44AD,
        )

    # Other Actions
    @commands.command(help="Roast someone")
    async def roast(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        embed = self.create_embed(
            "🔥 Roast",
            f"{member.mention}, {random.choice(FUN_CONTENT['roasts'])}",
            color=0xE74C3C,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Compliment someone")
    async def compliment(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        embed = self.create_embed(
            "💖 Compliment",
            f"{member.mention}, {random.choice(FUN_CONTENT['compliments'])}",
            color=0xFF69B4,
        )
        await send_v2(ctx, embed=embed)

    @commands.command(help="Fake hack someone")
    async def hack(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            embed = self.create_embed(
                "💻 Hack", "Who do you want to hack?", color=0xED4245
            )
            await send_v2(ctx, embed=embed)
            return

        embed = self.create_embed(
            "💻 Hack Sequence Initiated",
            f"Target: **{member.display_name}**...",
            color=0x2ECC71,
        )
        msg = await send_v2(ctx, embed=embed)

        await asyncio.sleep(1)
        embed.description = f"Finding IP address...\n`[192.168.{random.randint(0, 255)}.{random.randint(0, 255)}]`"
        await edit_v2(msg, embed=embed)

        await asyncio.sleep(1)
        embed.description += "\nBypassing firewall... `[SUCCESS]`"
        await edit_v2(msg, embed=embed)

        await asyncio.sleep(1)
        embed.description += "\nStealing Discord tokens... `[SUCCESS]`"
        await edit_v2(msg, embed=embed)

        await asyncio.sleep(1)
        embed.title = "✅ Hack Complete"
        embed.description += f"\n\nSuccessfully hacked **{member.display_name}**! Found {random.randint(10, 1000)} embarrassing messages."
        embed.color = 0x2ECC71
        await edit_v2(msg, embed=embed)


    @commands.command(name="blacklist", help="Admin only. Blacklist a user from using the bot entirely.")
    async def blacklist_cmd(self, ctx: commands.Context, target: discord.User, *, reason: str = "No reason provided."):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if not _is_blacklist_administrator(ctx):
            await ctx.send("You do not have permission to use this command.")
            return
        if _is_protected_blacklist_target(ctx, target):
            await ctx.send("You cannot blacklist this user.")
            return

        try:
            await asyncio.to_thread(ctx.bot.safety_store.add_blacklist, target.id, ctx.author.id, reason)
            if not hasattr(ctx.bot, "blacklisted_user_ids"):
                ctx.bot.blacklisted_user_ids = set()
            ctx.bot.blacklisted_user_ids.add(target.id)
            await ctx.send(f"✅ Successfully blacklisted **{target.display_name}** (`{target.id}`) from using the bot.\nReason: {reason}")
        except Exception as e:
            await ctx.send(f"❌ Failed to blacklist user: {e}")

    @commands.command(name="unblacklist", help="Admin only. Remove a user from the bot blacklist.")
    async def unblacklist_cmd(self, ctx: commands.Context, target: discord.User):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if not _is_blacklist_administrator(ctx):
            await ctx.send("You do not have permission to use this command.")
            return

        try:
            removed = await asyncio.to_thread(ctx.bot.safety_store.remove_blacklist, target.id)
            if removed:
                if hasattr(ctx.bot, "blacklisted_user_ids"):
                    ctx.bot.blacklisted_user_ids.discard(target.id)
                await ctx.send(f"✅ Successfully unblacklisted **{target.display_name}** (`{target.id}`). They can use the bot again.")
            else:
                await ctx.send(f"⚠️ **{target.display_name}** (`{target.id}`) is not currently blacklisted.")
        except Exception as e:
            await ctx.send(f"❌ Failed to unblacklist user: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(FunCommands(bot))
