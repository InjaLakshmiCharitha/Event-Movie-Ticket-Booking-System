import os
import json
import logging

import redis
from redis.exceptions import RedisError
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is missing from the .env file")


# Redis connection pool
redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
)

# Seat locking configuration
SEAT_LOCK_TTL = 600

# Cache configuration
DEFAULT_CACHE_TTL = 300


def check_redis() -> bool:
    """Check whether Redis is available."""
    try:
        return bool(redis_client.ping())
    except RedisError:
        logger.exception("Redis connection failed")
        return False


def cache_get(key: str):
    """Get JSON data from Redis cache."""
    try:
        cached_data = redis_client.get(f"cache:{key}")

        if cached_data is None:
            return None

        return json.loads(cached_data)

    except (RedisError, json.JSONDecodeError):
        logger.exception("Redis cache read failed")
        return None


def cache_set(
    key: str,
    value,
    ttl: int = DEFAULT_CACHE_TTL
) -> bool:
    """Save JSON data in Redis cache with expiration."""
    try:
        serialized_data = json.dumps(value, default=str)

        return bool(
            redis_client.set(
                f"cache:{key}",
                serialized_data,
                ex=ttl
            )
        )

    except (RedisError, TypeError, ValueError):
        logger.exception("Redis cache write failed")
        return False


def cache_delete(key: str) -> bool:
    """Delete a specific cache entry."""
    try:
        redis_client.delete(f"cache:{key}")
        return True

    except RedisError:
        logger.exception("Redis cache deletion failed")
        return False


def cache_delete_pattern(pattern: str) -> bool:
    """Delete cache entries matching a pattern."""
    try:
        keys = redis_client.scan_iter(
            match=f"cache:{pattern}",
            count=100
        )

        for key in keys:
            redis_client.delete(key)

        return True

    except RedisError:
        logger.exception("Redis pattern cache deletion failed")
        return False


RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])

if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end

local ttl = redis.call('TTL', KEYS[1])

if current > tonumber(ARGV[2]) then
    return ttl
end

return 0
"""


def enforce_rate_limit(
    key: str,
    max_requests: int = 5,
    window_seconds: int = 60
):
    """
    Allow a maximum number of requests within a time window.
    Raises HTTP 429 when the limit is exceeded.
    """
    try:
        retry_after = redis_client.eval(
            RATE_LIMIT_SCRIPT,
            1,
            f"rate_limit:{key}",
            window_seconds,
            max_requests
        )

        if retry_after and int(retry_after) > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Too many requests. Please try again later.",
                    "retry_after_seconds": int(retry_after)
                },
                headers={"Retry-After": str(int(retry_after))}
            )

    except HTTPException:
        raise
    except RedisError:
        logger.exception("Redis rate-limiting operation failed")
        raise HTTPException(
            status_code=503,
            detail="Rate limiting service is temporarily unavailable."
        )
