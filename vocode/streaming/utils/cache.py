import os
from cachetools import LRUCache
from redis import Redis


class RedisRenewableTTLCache:
    _redis_client = Redis(
        host=os.environ.get("REDISHOST", "localhost"),
        port=int(os.environ.get("REDISPORT", 6379)))
    _lru_cache = LRUCache(maxsize=2048)
    _ttl_in_seconds = int(os.environ.get("REDIS_TTL_IN_SECONDS", 60 * 60 * 24))

    def get(self, key):
        if key in self._lru_cache:
            self._redis_client.expire(key, self._ttl_in_seconds)
            return self._lru_cache[key]

        value = self._redis_client.getex(key, ex=self._ttl_in_seconds)
        if not value is None:
            self._lru_cache[key] = value

        return value

    def set(self, key, value):
        self._lru_cache[key] = value

        # TODO: in the future we could use pickle.dumps/pickle.loads for classes, with caveats
        if self.value_type_is_supported(value):
            self._redis_client.setex(key, self._ttl_in_seconds, value)

    def value_type_is_supported(self, value) -> bool:
        return (
            isinstance(value, bytes)
            or isinstance(value, str)
            or isinstance(value, int)
            or isinstance(value, float)
        )