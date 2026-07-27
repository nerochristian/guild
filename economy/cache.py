import time
from functools import wraps
from threading import Lock

def ttl_cache(maxsize=1024, ttl=60):
    """
    A lightweight, thread-safe TTL cache decorator for high-frequency database read methods.
    Does not require external dependencies like cachetools.
    """
    cache = {}
    lock = Lock()
    
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            # The first argument is 'self' (the store instance), which we ignore in the cache key
            key = (func.__name__, args, tuple(sorted(kwargs.items())))
            
            with lock:
                if key in cache:
                    result, expiry = cache[key]
                    if time.time() < expiry:
                        return result
                    else:
                        del cache[key]
            
            # Fetch from actual database (uncached)
            result = func(self, *args, **kwargs)
            
            with lock:
                # Eviction logic
                if len(cache) >= maxsize:
                    now = time.time()
                    expired = [k for k, v in cache.items() if v[1] < now]
                    for k in expired:
                        del cache[k]
                    if len(cache) >= maxsize:
                        # Fallback fast-clear if still full
                        cache.clear()
                
                cache[key] = (result, time.time() + ttl)
            return result
            
        # Provide a method to manually clear this specific cache if needed
        def clear_cache():
            with lock:
                cache.clear()
        wrapper.clear_cache = clear_cache
        
        return wrapper
    return decorator
