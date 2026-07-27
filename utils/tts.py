"""
TTS Utility - Ultra-Realistic Text to Speech for Voice Channels

Priority order for TTS engines:
1. ElevenLabs (MOST REALISTIC - requires API key in .env: ELEVENLABS_API_KEY)
2. Edge TTS (Microsoft's neural voices - FREE, very good quality)
3. gTTS (Google Translate voice - fallback)
"""

import asyncio
import os
import tempfile
import discord
import aiohttp
from typing import Optional
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# TTS ENGINE IMPORTS
# ═══════════════════════════════════════════════════════════════════════════

# Get API key from config
try:
    from config import Config
    ELEVENLABS_API_KEY = Config.ELEVENLABS_API_KEY
except Exception:
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# ElevenLabs - THE most realistic AI voices
ELEVENLABS_AVAILABLE = False
if ELEVENLABS_API_KEY:
    try:
        from elevenlabs.client import ElevenLabs
        from elevenlabs import play, save
        elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        ELEVENLABS_AVAILABLE = True
        print("✅ ElevenLabs TTS loaded - Ultra-realistic voices enabled!")
    except ImportError:
        elevenlabs_client = None
    except Exception as e:
        print(f"⚠️ ElevenLabs error: {e}")
        elevenlabs_client = None
else:
    elevenlabs_client = None

# Edge TTS - Microsoft's neural voices (FREE, very good)
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

# gTTS - Google Translate (fallback)
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# VOICE OPTIONS
# ═══════════════════════════════════════════════════════════════════════════

# ElevenLabs voices (most realistic)
ELEVENLABS_VOICES = {
    "adam": "pNInz6obpgDQGcFmaJgB",      # Deep, authoritative male
    "antoni": "ErXwobaYiN019PkySvjV",     # Warm friendly male
    "arnold": "VR6AewLTigWG4xSOukaG",     # Powerful, dramatic
    "josh": "TxGEqnHWrfWFTfGW9XjX",       # Young professional male
    "sam": "yoZ06aMxZJJ28mfd3POQ",        # Calm, clear male
    "rachel": "21m00Tcm4TlvDq8ikWAM",     # Professional female
    "domi": "AZnzlk1XvdvUeBnXmlld",       # Strong female
    "bella": "EXAVITQu4vr4xnSDxMaL",      # Soft, warm female
    "elli": "MF3mGyEYCl7XYWbV9V6O",       # Young female
}

# Edge TTS voices (Microsoft neural - FREE and excellent)
EDGE_VOICES = {
    # Male - Natural sounding
    "guy": "en-US-GuyNeural",             # Deep, professional (RECOMMENDED)
    "davis": "en-US-DavisNeural",         # Warm, friendly 
    "tony": "en-US-TonyNeural",           # Clear, announcer-style
    "jason": "en-US-JasonNeural",         # Young professional
    "christopher": "en-US-ChristopherNeural",  # Authoritative
    
    # Female - Natural sounding
    "aria": "en-US-AriaNeural",           # Professional
    "jenny": "en-US-JennyNeural",         # Friendly, warm
    "michelle": "en-US-MichelleNeural",   # Clear, professional
    "sara": "en-US-SaraNeural",           # Young, energetic
    
    # British accents
    "ryan": "en-GB-RyanNeural",           # British male
    "sonia": "en-GB-SoniaNeural",         # British female
}

# Default voice selection
DEFAULT_ELEVENLABS_VOICE = "adam"  # Deep, authoritative - perfect for moderation
DEFAULT_EDGE_VOICE = "en-US-GuyNeural"  # Best free alternative


# ═══════════════════════════════════════════════════════════════════════════
# CACHE CONFIG
# ═══════════════════════════════════════════════════════════════════════════

TTS_CACHE_DIR = Path(tempfile.gettempdir()) / "modbot_tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# TTS GENERATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

async def generate_elevenlabs_audio(text: str, voice: str = DEFAULT_ELEVENLABS_VOICE) -> Optional[str]:
    """Generate audio using ElevenLabs (most realistic)."""
    if not ELEVENLABS_AVAILABLE or not elevenlabs_client:
        return None
    
    # Get voice ID
    voice_id = ELEVENLABS_VOICES.get(voice.lower(), voice)
    
    # Create cache filename
    import hashlib
    text_hash = hashlib.md5(f"11labs_{text}_{voice_id}".encode()).hexdigest()[:12]
    output_path = TTS_CACHE_DIR / f"tts_11labs_{text_hash}.mp3"
    
    if output_path.exists():
        return str(output_path)
    
    try:
        # Generate audio using client
        audio = elevenlabs_client.generate(
            text=text,
            voice=voice_id,
            model="eleven_monolingual_v1"
        )
        
        # Save to file (audio is a generator)
        with open(output_path, "wb") as f:
            for chunk in audio:
                f.write(chunk)
        
        return str(output_path)
    except Exception as e:
        print(f"ElevenLabs TTS error: {e}")
        return None


async def generate_edge_audio(text: str, voice: str = DEFAULT_EDGE_VOICE, rate: str = "+0%", pitch: str = "+0Hz") -> Optional[str]:
    """Generate audio using Microsoft Edge TTS (free, very good quality)."""
    if not EDGE_TTS_AVAILABLE:
        return None
    
    # Resolve voice name
    voice_id = EDGE_VOICES.get(voice.lower(), voice)
    
    # Create cache filename
    import hashlib
    text_hash = hashlib.md5(f"edge_{text}_{voice_id}_{rate}_{pitch}".encode()).hexdigest()[:12]
    output_path = TTS_CACHE_DIR / f"tts_edge_{text_hash}.mp3"
    
    if output_path.exists():
        return str(output_path)
    
    try:
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))
        return str(output_path)
    except Exception as e:
        print(f"Edge TTS error: {e}")
        return None


async def generate_gtts_audio(text: str) -> Optional[str]:
    """Generate audio using gTTS (fallback, less realistic)."""
    if not GTTS_AVAILABLE:
        return None
    
    import hashlib
    text_hash = hashlib.md5(f"gtts_{text}".encode()).hexdigest()[:12]
    output_path = TTS_CACHE_DIR / f"tts_gtts_{text_hash}.mp3"
    
    if output_path.exists():
        return str(output_path)
    
    try:
        tts = gTTS(text=text, lang='en')
        tts.save(str(output_path))
        return str(output_path)
    except Exception as e:
        print(f"gTTS error: {e}")
        return None


async def generate_tts_audio(text: str, voice: str = "auto", rate: str = "+0%", pitch: str = "+0Hz") -> Optional[str]:
    """
    Generate TTS audio using the best available engine.
    
    Priority: ElevenLabs > Edge TTS > gTTS
    
    Args:
        text: Text to speak
        voice: Voice name (auto-selects best for each engine)
        rate: Speech rate for Edge TTS
        pitch: Pitch adjustment for Edge TTS
    
    Returns:
        Path to audio file (MP3)
    """
    
    # Try ElevenLabs first (most realistic)
    if ELEVENLABS_AVAILABLE:
        audio_path = await generate_elevenlabs_audio(text, voice if voice != "auto" else DEFAULT_ELEVENLABS_VOICE)
        if audio_path:
            return audio_path
    
    # Try Edge TTS (free, very good)
    if EDGE_TTS_AVAILABLE:
        edge_voice = voice if voice != "auto" else DEFAULT_EDGE_VOICE
        audio_path = await generate_edge_audio(text, edge_voice, rate, pitch)
        if audio_path:
            return audio_path
    
    # Fallback to gTTS
    if GTTS_AVAILABLE:
        audio_path = await generate_gtts_audio(text)
        if audio_path:
            return audio_path
    
    return None


# ═══════════════════════════════════════════════════════════════════════════
# VOICE CHANNEL PLAYBACK
# ═══════════════════════════════════════════════════════════════════════════

async def speak_in_vc(
    voice_client: discord.VoiceClient,
    text: str,
    voice: str = "auto",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    wait: bool = True
) -> bool:
    """
    Make the bot speak in a voice channel using the most realistic TTS available.
    
    Args:
        voice_client: Connected VoiceClient
        text: Text to speak
        voice: Voice to use (auto = best available)
        rate: Speech rate (Edge TTS)
        pitch: Pitch adjustment (Edge TTS)
        wait: Wait for audio to finish
    
    Returns:
        True if successful
    """
    if not voice_client or not voice_client.is_connected():
        return False
    
    # Generate audio with best available engine
    audio_path = await generate_tts_audio(text, voice, rate, pitch)
    if not audio_path:
        return False
    
    try:
        # Create audio source
        audio_source = discord.FFmpegPCMAudio(audio_path)
        
        # Stop any current playback
        if voice_client.is_playing():
            voice_client.stop()
        
        # Play the audio
        voice_client.play(audio_source)
        
        # Wait for completion if requested
        if wait:
            while voice_client.is_playing():
                await asyncio.sleep(0.1)
        
        return True
    except Exception as e:
        print(f"Voice playback error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# ANNOUNCEMENT MESSAGES
# ═══════════════════════════════════════════════════════════════════════════

class Announcements:
    """Pre-defined TTS announcements for moderation actions."""
    
    # Presence Check - Channel
    PRESENCE_CHECK_START = (
        "Attention. This is a moderation presence check. "
        "Please confirm you are here by clicking the button in your DMs. "
        "You have 60 seconds to respond, or you will be disconnected."
    )
    
    PRESENCE_CHECK_START_SHORT = "Presence check. Check your DMs to confirm you're here."
    
    PRESENCE_CHECK_COMPLETE = "Presence check complete. Thank you for your cooperation."
    
    PRESENCE_CHECK_ALL_PRESENT = "Presence check complete. All users have confirmed their presence."
    
    # Presence Check - Single User
    USER_CHECK_START = "Moderation check. {}, please confirm you are present by checking your direct messages."
    
    USER_CHECK_THANKS = "Thank you for confirming your presence."
    
    USER_CHECK_TIMEOUT = "No response received. Disconnecting user."
    
    # Dynamic messages
    @staticmethod
    def presence_kicks(count: int) -> str:
        if count == 0:
            return Announcements.PRESENCE_CHECK_ALL_PRESENT
        elif count == 1:
            return "Presence check complete. One user was disconnected for not responding."
        else:
            return f"Presence check complete. {count} users were disconnected for not responding."


# ═══════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def get_tts_engine() -> str:
    """Get the currently active TTS engine name."""
    if ELEVENLABS_AVAILABLE:
        return "ElevenLabs (Ultra-Realistic)"
    elif EDGE_TTS_AVAILABLE:
        return "Microsoft Edge TTS"
    elif GTTS_AVAILABLE:
        return "Google TTS"
    else:
        return "None"


async def cleanup_old_cache(max_age_hours: int = 24):
    """Clean up old TTS cache files."""
    import time
    
    now = time.time()
    max_age_seconds = max_age_hours * 3600
    
    cleaned = 0
    for file in TTS_CACHE_DIR.glob("tts_*.mp3"):
        try:
            if now - file.stat().st_mtime > max_age_seconds:
                file.unlink()
                cleaned += 1
        except Exception:
            pass
    
    return cleaned
