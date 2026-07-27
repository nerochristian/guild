import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent.parent
PET_EMOJI_DIR = BASE_DIR / "assets" / "pets" / "emojis"

load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

client = discord.Client(intents=discord.Intents.default())


def load_pet_emojis() -> dict[str, bytes]:
    return {
        f"pet_{path.stem}": path.read_bytes()
        for path in sorted(PET_EMOJI_DIR.glob("*.png"))
    }


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    if TARGET_GUILD_ID:
        guild = client.get_guild(int(TARGET_GUILD_ID))
    else:
        guild = client.guilds[0] if client.guilds else None

    if guild is None:
        print("Target guild was not found.")
        await client.close()
        return

    emoji_data = load_pet_emojis()
    if not emoji_data:
        print(f"No pet emoji PNGs found in {PET_EMOJI_DIR}.")
        await client.close()
        return

    existing = {emoji.name: emoji for emoji in guild.emojis}
    print(f"Syncing {len(emoji_data)} pet emoji(s) to {guild.name}...")

    for name, data in emoji_data.items():
        old = existing.get(name)
        if old is not None:
            print(f"Deleting existing emoji {name}...")
            await old.delete()

        print(f"Creating emoji {name}...")
        try:
            await guild.create_custom_emoji(name=name, image=data)
        except Exception as exc:
            print(f"Failed to create {name}: {exc}")

    print("Pet emoji setup complete.")
    await client.close()


if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_BOT_TOKEN must be set in guild/.env.")
        sys.exit(1)
    client.run(TOKEN)
