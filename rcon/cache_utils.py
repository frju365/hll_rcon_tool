import redis
import simplejson
import logging
import os
import functools
from cachetools.func import ttl_cache as cachetools_ttl_cache

#REDIS_POOL = redis.BlockingConnectionPool(max_connections=20, host='localhost', port=6379, db=0)
logger = logging.getLogger(__name__)

_REDIS_POOL = None


class RedisCached:
    def __init__(self, pool, ttl_seconds, function, is_method=False, cache_falsy=True, serializer=simplejson.dumps, deserializer=simplejson.loads):
        self.red = redis.Redis(connection_pool=pool)
        self.function = function
        self.serializer = serializer
        self.deserializer = deserializer
        self.ttl_seconds = ttl_seconds
        self.is_method = is_method
        self.cache_falsy = cache_falsy

    @property
    def key_prefix(self):
        return self.function.__name__

    def key(self, *args, **kwargs):
        if self.is_method:
            args = args[1:]
        params = self.serializer({'args': args, "kwargs": kwargs})
        return f"{self.key_prefix}__{params}"

    @property
    def __name__(self):
        return self.function.__name__

    @property
    def __wrapped__(self):
        return self.function

    def __call__(self, *args, **kwargs):
        val = None
        key = self.key(*args, **kwargs)
        try:
            val = self.red.get(key)
        except redis.exceptions.RedisError:
            logger.exception("Unable to use cache")

        if val is not None:
            logger.debug("Cache HIT for %s", self.key(*args, **kwargs))
            return self.deserializer(val)

        logger.debug("Cache MISS for %s", self.key(*args, **kwargs))
        val = self.function(*args, **kwargs)

        if not val and not self.cache_falsy:
            logger.debug("Caching falsy result is disabled for %s", self.__name__)
            return val 

        try:
            self.red.setex(key, self.ttl_seconds, self.serializer(val))
            logger.debug("Cache SET for %s", self.key(*args, **kwargs))
        except redis.exceptions.RedisError:
            logger.exception("Unable to set cache")

        return val

    def clear_all(self):
        try:
            keys = list(self.red.scan_iter(match=f"{self.key_prefix}*"))
            if keys:
                self.red.delete(*keys)
        except redis.exceptions.RedisError:
            logger.exception("Unable to clear cache")
        else:
            logger.debug("Cache CLEARED for %s", keys)


def ttl_cache(ttl, *args, is_method=True, cache_falsy=True, **kwargs):
    global _REDIS_POOL
    redis_url = os.getenv('REDIS_URL')
    if not redis_url:
        logger.warning("REDIS_URL is not set falling back to memory cache")
        return cachetools_ttl_cache(*args, ttl=ttl, **kwargs)
    if _REDIS_POOL is None:
        logger.warning("Redis pool initializing")
        _REDIS_POOL = redis.ConnectionPool.from_url(
            redis_url, max_connections=10, socket_connect_timeout=5,
            socket_timeout=5, decode_responses=True
        )

    def decorator(func):
        cached_func = RedisCached(
            _REDIS_POOL, ttl, function=func, is_method=is_method, cache_falsy=cache_falsy)

        def wrapper(*args, **kwargs):
            # Re-wrapping to preserve function signature
            return cached_func(*args, **kwargs)

        functools.update_wrapper(wrapper, func)
        wrapper.cache_clear = cached_func.clear_all
        return wrapper
    return decorator
