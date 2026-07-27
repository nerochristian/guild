"""
Redis-backed Cache Layer with Automatic In-Memory Fallback

Provides drop-in replacements for all cache classes in utils/cache.py.
When REDIS_URL is set and Redis is reachable, data is stored in Redis for
cluster-ready, multi-process persistence.  When Redis is unavailable the
module transparently falls back to the original in-memory implementations,
so callers never need to handle the difference.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypeVar

from utils.cache import (
    TTLCache as MemoryTTLCache,
    SnipeCache as MemorySnipeCache,
    PrefixCache as MemoryPrefixCache,
    RateLimiter as MemoryRateLimiter,
)

logger = logging.getLogger("ModBot.RedisCache")
T = TypeVar("T")

_redis_client: Optional[Any] = None
_redis_available: bool = False


async def _get_redis():
    """Lazily initialize and return the shared Redis client, or None."""
    global _redis_client, _redis_available

    if _redis_client is not None:
        return _redis_client

    url = (os.getenv("REDIS_URL") or os.getenv("REDIS_URI") or "").strip()
    if not url:
        _redis_available = False
        return None

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
        # Verify the connection is live.
        await client.ping()
        _redis_client = client
        _redis_available = True
        logger.info("Redis cache connected: %s", url.split("@")[-1] if "@" in url else url[:40])
        return client
    except Exception as exc:
        _redis_available = False
        logger.warning("Redis unavailable, falling back to in-memory caches: %s", exc)
        return None


# =============================================================================
# Redis-backed TTLCache
# =============================================================================


class RedisTTLCache:
    """TTL cache backed by Redis hashes + per-key expiry."""

    def __init__(self, *, namespace: str, ttl: int = 300, max_size: int = 1000):
        self._ns = f"modbot:cache:{namespace}"
        self.ttl = ttl
        self.max_size = max_size
        self._hits = 0
        self._misses = 0

    async def get(self, key: Any) -> Optional[Any]:
        r = await _get_redis()
        if r is None:
            return None
        try:
            raw = await r.get(f"{self._ns}:{key}")
            if raw is None:
                self._misses += 1
                return None
            self._hits += 1
            return json.loads(raw)
        except Exception:
            self._misses += 1
            return None

    async def set(self, key: Any, value: Any, ttl: Optional[int] = None) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            await r.setex(
                f"{self._ns}:{key}",
                ttl if ttl is not None else self.ttl,
                json.dumps(value, default=str),
            )
        except Exception as exc:
            logger.debug("Redis set failed for %s: %s", self._ns, exc)

    async def delete(self, key: Any) -> bool:
        r = await _get_redis()
        if r is None:
            return False
        try:
            deleted = await r.delete(f"{self._ns}:{key}")
            return deleted > 0
        except Exception:
            return False

    async def clear(self) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            cursor = "0"
            while cursor:
                cursor, keys = await r.scan(cursor=cursor, match=f"{self._ns}:*", count=200)
                if keys:
                    await r.delete(*keys)
                if cursor == "0" or cursor == 0:
                    break
        except Exception as exc:
            logger.debug("Redis clear failed for %s: %s", self._ns, exc)

    async def cleanup_expired(self) -> int:
        # Redis handles TTL natively; nothing to clean.
        return 0

    def get_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "backend": "redis",
            "namespace": self._ns,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.2f}%",
            "ttl": self.ttl,
        }


# =============================================================================
# Redis-backed SnipeCache
# =============================================================================


class RedisSnipeCache:
    """Sniped message cache backed by Redis."""

    def __init__(self, *, max_age_seconds: int = 300, max_size: int = 500):
        self._ns = "modbot:snipe"
        self.max_age = max_age_seconds
        self.max_size = max_size

    async def add(self, channel_id: int, message_data: Dict[str, Any]) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            await r.setex(
                f"{self._ns}:{channel_id}",
                self.max_age,
                json.dumps(message_data, default=str),
            )
        except Exception as exc:
            logger.debug("Redis snipe add failed: %s", exc)

    async def get(self, channel_id: int) -> Optional[Dict[str, Any]]:
        r = await _get_redis()
        if r is None:
            return None
        try:
            raw = await r.get(f"{self._ns}:{channel_id}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    async def clear(self) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            cursor = "0"
            while cursor:
                cursor, keys = await r.scan(cursor=cursor, match=f"{self._ns}:*", count=200)
                if keys:
                    await r.delete(*keys)
                if cursor == "0" or cursor == 0:
                    break
        except Exception:
            pass


# =============================================================================
# Redis-backed PrefixCache
# =============================================================================


class RedisPrefixCache:
    """Guild prefix cache backed by Redis."""

    def __init__(self, *, ttl: int = 600):
        self._ns = "modbot:prefix"
        self._ttl = ttl

    async def get(self, guild_id: int) -> Optional[str]:
        r = await _get_redis()
        if r is None:
            return None
        try:
            return await r.get(f"{self._ns}:{guild_id}")
        except Exception:
            return None

    async def set(self, guild_id: int, prefix: str) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            await r.setex(f"{self._ns}:{guild_id}", self._ttl, prefix)
        except Exception:
            pass

    async def invalidate(self, guild_id: int) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            await r.delete(f"{self._ns}:{guild_id}")
        except Exception:
            pass

    async def clear(self) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            cursor = "0"
            while cursor:
                cursor, keys = await r.scan(cursor=cursor, match=f"{self._ns}:*", count=200)
                if keys:
                    await r.delete(*keys)
                if cursor == "0" or cursor == 0:
                    break
        except Exception:
            pass


# =============================================================================
# Redis-backed RateLimiter
# =============================================================================


class RedisRateLimiter:
    """
    Rate limiter using Redis sorted sets for sliding window tracking.
    Each key maps to a sorted set where scores are Unix timestamps.
    """

    def __init__(self, *, max_calls: int, window_seconds: int, namespace: str = "global"):
        self.max_calls = max_calls
        self.window = window_seconds
        self._ns = f"modbot:ratelimit:{namespace}"

    async def is_rate_limited(self, key: Any) -> tuple[bool, float]:
        r = await _get_redis()
        if r is None:
            return False, 0.0
        try:
            redis_key = f"{self._ns}:{key}"
            now = datetime.now(timezone.utc).timestamp()
            window_start = now - self.window

            # Remove expired entries and count remaining.
            pipe = r.pipeline()
            pipe.zremrangebyscore(redis_key, "-inf", window_start)
            pipe.zcard(redis_key)
            pipe.zrange(redis_key, 0, 0, withscores=True)
            results = await pipe.execute()

            count = results[1]
            if count >= self.max_calls:
                oldest = results[2]
                if oldest:
                    oldest_time = oldest[0][1]
                    retry_after = self.window - (now - oldest_time)
                    return True, max(0.0, retry_after)
                return True, float(self.window)
            return False, 0.0
        except Exception:
            return False, 0.0

    async def record_call(self, key: Any) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            redis_key = f"{self._ns}:{key}"
            now = datetime.now(timezone.utc).timestamp()
            pipe = r.pipeline()
            pipe.zadd(redis_key, {str(now): now})
            pipe.expire(redis_key, self.window + 10)
            await pipe.execute()
        except Exception:
            pass

    async def reset(self, key: Any) -> None:
        r = await _get_redis()
        if r is None:
            return
        try:
            await r.delete(f"{self._ns}:{key}")
        except Exception:
            pass

    async def cleanup(self) -> None:
        # Redis TTLs handle cleanup automatically.
        pass


# =============================================================================
# Factory — returns Redis or Memory implementations
# =============================================================================


async def create_cache_backend() -> dict[str, Any]:
    """
    Probe Redis availability and return a dict of cache instances.

    Returns:
        {
            "backend": "redis" | "memory",
            "snipe_cache": SnipeCache,
            "edit_snipe_cache": SnipeCache,
            "prefix_cache": PrefixCache,
        }
    """
    r = await _get_redis()

    if r is not None:
        logger.info("Using Redis-backed caches.")
        return {
            "backend": "redis",
            "snipe_cache": RedisSnipeCache(max_age_seconds=300, max_size=500),
            "edit_snipe_cache": RedisSnipeCache(max_age_seconds=300, max_size=500),
            "prefix_cache": RedisPrefixCache(ttl=600),
            "automod_history": RedisTTLCache(namespace="automod_history", ttl=2592000, max_size=10000),
        }

    logger.info("Using in-memory caches (no REDIS_URL configured).")
    return {
        "backend": "memory",
        "snipe_cache": MemorySnipeCache(max_age_seconds=300, max_size=500),
        "edit_snipe_cache": MemorySnipeCache(max_age_seconds=300, max_size=500),
        "prefix_cache": MemoryPrefixCache(ttl=600),
        "automod_history": MemoryTTLCache(ttl=2592000, max_size=10000),
    }
