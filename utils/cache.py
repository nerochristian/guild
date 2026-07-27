"""
Advanced Caching Utilities with TTL Support
Prevents memory leaks and provides efficient data caching
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, TypeVar, Generic
from collections import OrderedDict
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')


class CachedItem(Generic[T]):
    """A cached item with expiration time"""
    
    def __init__(self, value: T, ttl: int = 300):
        """
        Args:
            value: The value to cache
            ttl: Time to live in seconds (default: 5 minutes)
        """
        self.value = value
        self.created_at = datetime.now()
        self.expires_at = self.created_at + timedelta(seconds=ttl)
    
    def is_expired(self) -> bool:
        """Check if the cached item has expired"""
        return datetime.now() > self.expires_at
    
    def get(self) -> Optional[T]:
        """Get the value if not expired, otherwise None"""
        if self.is_expired():
            return None
        return self.value


class TTLCache(Generic[T]):
    """
    Time-To-Live Cache with automatic expiration
    Thread-safe and memory-efficient
    """
    
    def __init__(self, ttl: int = 300, max_size: int = 1000):
        """
        Args:
            ttl: Default time to live in seconds
            max_size: Maximum number of items to cache (LRU eviction)
        """
        self.ttl = ttl
        self.max_size = max_size
        self._cache: OrderedDict[Any, CachedItem[T]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
    
    async def get(self, key: Any) -> Optional[T]:
        """Get a value from cache"""
        async with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            item = self._cache[key]
            if item.is_expired():
                del self._cache[key]
                self._misses += 1
                return None
            
            # Move to end (LRU)
            self._cache.move_to_end(key)
            self._hits += 1
            return item.value
    
    async def set(self, key: Any, value: T, ttl: Optional[int] = None) -> None:
        """Set a value in cache"""
        async with self._lock:
            # Evict oldest if at max size
            if len(self._cache) >= self.max_size and key not in self._cache:
                self._cache.popitem(last=False)
            
            # Use custom TTL or default
            item_ttl = ttl if ttl is not None else self.ttl
            self._cache[key] = CachedItem(value, item_ttl)
            self._cache.move_to_end(key)
    
    async def delete(self, key: Any) -> bool:
        """Delete a key from cache"""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    async def clear(self) -> None:
        """Clear all cached items"""
        async with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
    
    async def cleanup_expired(self) -> int:
        """Remove all expired items and return count removed"""
        async with self._lock:
            expired_keys = [
                key for key, item in self._cache.items() 
                if item.is_expired()
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.2f}%",
            "ttl": self.ttl
        }
    
    async def start_cleanup_task(self, interval: int = 60):
        """Start background task to clean up expired items"""
        while True:
            await asyncio.sleep(interval)
            removed = await self.cleanup_expired()
            if removed > 0:
                logger.debug(f"Cache cleanup: removed {removed} expired items")


class SnipeCache:
    """
    Specialized cache for deleted/edited messages
    Automatically expires old entries
    """
    
    def __init__(self, max_age_seconds: int = 300, max_size: int = 500):
        """
        Args:
            max_age_seconds: Maximum age of cached messages (default: 5 minutes)
            max_size: Maximum number of messages to cache
        """
        self.max_age = max_age_seconds
        self.max_size = max_size
        self._cache: Dict[int, CachedItem[Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
    
    async def add(self, channel_id: int, message_data: Dict[str, Any]) -> None:
        """Add a sniped message to cache"""
        async with self._lock:
            # Enforce max size (FIFO)
            if len(self._cache) >= self.max_size:
                # Remove oldest entry
                oldest_key = min(
                    self._cache.keys(),
                    key=lambda k: self._cache[k].created_at
                )
                del self._cache[oldest_key]
            
            self._cache[channel_id] = CachedItem(message_data, self.max_age)
    
    async def get(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Get a sniped message from cache"""
        async with self._lock:
            if channel_id not in self._cache:
                return None
            
            item = self._cache[channel_id]
            if item.is_expired():
                del self._cache[channel_id]
                return None
            
            return item.value
    
    async def clear(self) -> None:
        """Clear all cached messages"""
        async with self._lock:
            self._cache.clear()


class PrefixCache:
    """
    Cache for guild prefixes with automatic refresh
    """
    
    def __init__(self, ttl: int = 600):
        """
        Args:
            ttl: Time to live for cached prefixes (default: 10 minutes)
        """
        self._cache = TTLCache[str](ttl=ttl, max_size=10000)
    
    async def get(self, guild_id: int) -> Optional[str]:
        """Get cached prefix for a guild"""
        return await self._cache.get(guild_id)
    
    async def set(self, guild_id: int, prefix: str) -> None:
        """Set cached prefix for a guild"""
        await self._cache.set(guild_id, prefix)
    
    async def invalidate(self, guild_id: int) -> None:
        """Invalidate cached prefix for a guild"""
        await self._cache.delete(guild_id)
    
    async def clear(self) -> None:
        """Clear all cached prefixes"""
        await self._cache.clear()


class ChannelCache:
    """
    Cache for channel IDs with validation
    Used by logging system
    """
    
    def __init__(self, ttl: int = 300):
        self._cache = TTLCache[int](ttl=ttl, max_size=5000)
    
    async def get(self, guild_id: int, log_type: str) -> Optional[int]:
        """Get cached channel ID"""
        key = f"{guild_id}:{log_type}"
        return await self._cache.get(key)
    
    async def set(self, guild_id: int, log_type: str, channel_id: Optional[int]) -> None:
        """Set cached channel ID"""
        key = f"{guild_id}:{log_type}"
        if channel_id is None:
            await self._cache.delete(key)
        else:
            await self._cache.set(key, channel_id)
    
    async def invalidate(self, guild_id: int, log_type: str) -> None:
        """Invalidate cached channel"""
        key = f"{guild_id}:{log_type}"
        await self._cache.delete(key)
    
    async def clear_guild(self, guild_id: int) -> None:
        """Clear all cached channels for a guild"""
        # This is a simplified version - in production you'd want to track by guild
        await self._cache.clear()


class RateLimiter:
    """
    Rate limiter using sliding window algorithm
    """
    
    def __init__(self, max_calls: int, window_seconds: int):
        """
        Args:
            max_calls: Maximum number of calls allowed in the window
            window_seconds: Time window in seconds
        """
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: Dict[Any, list[float]] = {}
        self._lock = asyncio.Lock()
    
    async def is_rate_limited(self, key: Any) -> tuple[bool, float]:
        """
        Check if key is rate limited
        
        Returns:
            (is_limited, retry_after_seconds)
        """
        async with self._lock:
            now = datetime.now().timestamp()
            
            # Initialize or clean old calls
            if key not in self._calls:
                self._calls[key] = []
            else:
                # Remove calls outside the window
                self._calls[key] = [
                    call_time for call_time in self._calls[key]
                    if now - call_time < self.window
                ]
            
            # Check if rate limited
            if len(self._calls[key]) >= self.max_calls:
                oldest_call = self._calls[key][0]
                retry_after = self.window - (now - oldest_call)
                return True, max(0, retry_after)
            
            return False, 0
    
    async def record_call(self, key: Any) -> None:
        """Record a call for rate limiting"""
        async with self._lock:
            now = datetime.now().timestamp()
            if key not in self._calls:
                self._calls[key] = []
            self._calls[key].append(now)
    
    async def reset(self, key: Any) -> None:
        """Reset rate limit for a key"""
        async with self._lock:
            if key in self._calls:
                del self._calls[key]
    
    async def cleanup(self) -> None:
        """Clean up old entries"""
        async with self._lock:
            now = datetime.now().timestamp()
            for key in list(self._calls.keys()):
                self._calls[key] = [
                    call_time for call_time in self._calls[key]
                    if now - call_time < self.window
                ]
                # Remove empty entries
                if not self._calls[key]:
                    del self._calls[key]
