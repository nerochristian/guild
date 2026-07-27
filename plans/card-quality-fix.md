# Card Rendering Quality Fix

## Root Causes

1. **Balance card is 800×200** — too small. Background images get crushed into a tiny canvas, losing all detail.
2. **`.bal` fetches avatar at only 128px** — then `_circle_avatar` upscales it to 160px. Starting from 128px means you're stretching a low-res source, visible as pixelation.
3. **Profile card is 700×295 at 2x = 1400×590** — reasonable but still small for Discord embeds.
4. **Circle mask uses 4x supersample** — decent but edges still show slight stepping at display sizes >120px.

## Changes

### 1. Upgrade balance card to 1600×400 with 2x rendering
**File:** `guild/economy_system.py`, `_generate_bal_card`

- Change `W, H = 800, 200` to `W, H = 1600, 400` (2x in each dimension)
- Scale all element positions by 2x (avatar position, font sizes, glow, etc.)
- Avatar size from 160 → 320
- Font sizes doubled (14→28, 18→36, 40→80)
- Glow ellipse coordinates doubled
- Accent ring thickness doubled
- This makes the background image fill a 1600×400 canvas instead of 800×200 — 4x more pixels of detail

### 2. Upgrade avatar fetch from 128px to 512px
**File:** `guild/economy_system.py`, `bal_cmd`

- Change `with_size(128)` to `with_size(512)`
- Discord supports up to 1024px avatars. 512px is the sweet spot: high quality without excessive bandwidth.

### 3. Upgrade circle rendering to 6x supersample
**File:** `guild/level_system.py`, `_circle_avatar`

- Change `render_size = size * 4` to `render_size = size * 6`
- 6x supersample on a 320px circle means render at 1920px then downscale — very clean edges

### 4. Reduce background dark overlay opacity
**File:** `guild/economy_system.py`, `_generate_bal_card`

- Change custom bg overlay alpha from 142 to 100 (was too dark, hiding the background)
- Change default bg overlay alpha from 225 to 200

### 5. Upgrade profile card base resolution from 700×295 to 1050×442
**File:** `guild/level_system.py`, `_generate_rank_card`

- Change `CARD_WIDTH, CARD_HEIGHT = 700, 295` to `1050, 442`
- Scale all element positions proportionally
- Avatar size from 86*scale to 129*scale (3× base = 258px at 2x render)
- This gives 2100×884 final output instead of 1400×590

## Files Modified
- `guild/economy_system.py` — `_generate_bal_card`, `bal_cmd`
- `guild/level_system.py` — `_circle_avatar`, `_generate_rank_card`

## Verification
- `python -m py_compile guild/economy_system.py`
- `python -m py_compile guild/level_system.py`
- Visual: `.bal` and `.profile` should show sharper backgrounds, crisp avatar circles, no pixelation
