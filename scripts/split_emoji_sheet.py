from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


EMOJI_NAMES = [
    "valk_success",
    "valk_error",
    "valk_warn",
    "valk_loading",
    "valk_apply",
    "valk_trophy",
    "valk_ticket",
    "valk_link",
    "valk_lock",
    "valk_ticket_support",
]


def _trim_alpha(image: Image.Image) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    return image.crop(bbox) if bbox else image


def _make_transparent_icon(tile: Image.Image, *, size: int) -> Image.Image:
    tile = tile.convert("RGBA")

    # The sheet has white icons/glow on a dark background. Treat bright pixels as
    # icon/glow opacity and discard the dark background.
    grayscale = tile.convert("L")
    alpha = grayscale.point(
        lambda value: 0 if value < 28 else min(255, int((value - 28) * 1.35))
    )
    white = Image.new("RGBA", tile.size, (255, 255, 255, 255))
    icon = Image.new("RGBA", tile.size, (255, 255, 255, 0))
    icon.paste(white, mask=alpha)
    icon = _trim_alpha(icon)

    icon.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    x = (size - icon.width) // 2
    y = (size - icon.height) // 2
    canvas.alpha_composite(icon, (x, y))
    return canvas


def split_sheet(input_path: Path, output_dir: Path, *, size: int = 128) -> list[Path]:
    sheet = Image.open(input_path).convert("RGBA")
    width, height = sheet.size
    cols = 5
    rows = 2
    tile_w = width // cols
    tile_h = height // rows

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for index, name in enumerate(EMOJI_NAMES):
        row = index // cols
        col = index % cols
        left = col * tile_w
        upper = row * tile_h
        right = width if col == cols - 1 else (col + 1) * tile_w
        lower = height if row == rows - 1 else (row + 1) * tile_h

        tile = sheet.crop((left, upper, right, lower))
        icon = _make_transparent_icon(tile, size=size)
        output_path = output_dir / f"{name}.png"
        icon.save(output_path, optimize=True)
        written.append(output_path)

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a 2x5 emoji icon sheet into Discord emoji PNGs."
    )
    parser.add_argument(
        "input", nargs="?", default="image.png", help="Input sheet path."
    )
    parser.add_argument("--out", default="assets/emojis", help="Output directory.")
    parser.add_argument(
        "--size", type=int, default=128, help="Output square size in pixels."
    )
    args = parser.parse_args()

    written = split_sheet(Path(args.input), Path(args.out), size=args.size)
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
