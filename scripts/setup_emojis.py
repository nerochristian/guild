import os
import sys
import tempfile
from pathlib import Path
import discord
from dotenv import load_dotenv

from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM

# Ensure we use the correct .env
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)
BASE_DIR = Path(__file__).parent.parent
EMOJI_ASSET_DIR = BASE_DIR / "assets" / "emojis"
LEGACY_EMOJI_NAMES = {"enzo_ticket_support"}

ICONS = {
    "valk_success": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>',
    "valk_error": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
    "valk_warn": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>',
    "valk_loading": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>',
    "valk_apply": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"></path><polyline points="14 2 14 8 20 8"></polyline><path d="M16 13H8"></path><path d="M16 17H8"></path><path d="M10 9H8"></path></svg>',
    "valk_trophy": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"></path><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"></path><path d="M4 22h16"></path><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"></path><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"></path><path d="M18 2H6v7a6 6 0 0 0 12 0V2z"></path></svg>',
    "valk_ticket": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V4a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v3"></path><rect x="2" y="7" width="20" height="13" rx="2" ry="2"></rect><path d="M16 20v-3a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v3"></path></svg>',
    "valk_link": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>',
    "valk_lock": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>',
    "valk_folder": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>',
}

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

client = discord.Client(intents=discord.Intents.default())


def load_asset_emojis() -> dict[str, bytes]:
    pngs = sorted(EMOJI_ASSET_DIR.glob("*.png"))
    return {path.stem: path.read_bytes() for path in pngs}


def render_svg_emojis() -> dict[str, bytes]:
    png_data_map = {}

    for name, svg_data in ICONS.items():
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False, mode="w") as f:
            f.write(svg_data)
            tmp_path = f.name

        try:
            drawing = svg2rlg(tmp_path)
            import io

            buf = io.BytesIO()
            # bg=0x1000000 yields transparent background
            renderPM.drawToFile(drawing, buf, fmt="PNG", bg=0x1000000)
            png_data_map[name] = buf.getvalue()
        except Exception as e:
            print(f"Failed generating {name}: {e}")
        finally:
            os.remove(tmp_path)

    return png_data_map


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    if not client.guilds:
        print("Bot is not in any guilds!")
        await client.close()
        return

    guild = client.guilds[0]
    print(f"Targeting guild: {guild.name}")

    png_data_map = load_asset_emojis()
    if png_data_map:
        print(f"Loaded {len(png_data_map)} PNG emoji asset(s) from {EMOJI_ASSET_DIR}.")
    else:
        print("No PNG emoji assets found. Generating fallback SVG emojis...")
        png_data_map = render_svg_emojis()

    print(f"Syncing {len(png_data_map)} emoji(s) to guild...")

    existing_emojis = {e.name: e for e in guild.emojis}

    for legacy_name in LEGACY_EMOJI_NAMES:
        legacy = existing_emojis.get(legacy_name)
        if legacy is not None:
            print(f"Deleting legacy emoji {legacy_name}...")
            try:
                await legacy.delete()
            except Exception as e:
                print(f"-> Failed to delete legacy emoji {legacy_name}: {e}")

    for name, data in png_data_map.items():
        if name in existing_emojis:
            print(f"Deleting existing emoji {name}...")
            await existing_emojis[name].delete()

        print(f"Creating new custom emoji {name}...")
        try:
            await guild.create_custom_emoji(name=name, image=data)
            print(f"-> Created {name} successfully.")
        except Exception as e:
            print(f"-> Failed to create {name}: {e}")

    print("Emoji setup complete! You can safely close this script.")
    await client.close()


if __name__ == "__main__":
    if not TOKEN:
        print("Please ensure DISCORD_BOT_TOKEN is set in your .env file.")
        sys.exit(1)

    print("Starting emoji sync process...")
    client.run(TOKEN)
