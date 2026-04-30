"""Redis-backed response cache with distributed stampede protection.

Uses a SET NX lock so that only one replica executes an LLM call for a
given cache key at a time. Other replicas wait and then read the result
written by the winner, avoiding redundant (and billed) LLM calls.
"""

import asyncio
import logging
from typing import Any, Callable, Awaitable, Optional, Tuple

import redis.asyncio as aioredis

from src.config import REDIS_URL, TASK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

CACHE_TTL = 3600   # seconds — cached results expire after 1 hour
LOCK_TTL = 60      # seconds — lock auto-expires if the holder crashes
LOCK_POLL = 0.25   # seconds — poll interval while waiting for lock

_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def get_cached(key: str) -> Optional[str]:
    try:
        return await get_redis().get(key)
    except Exception as exc:
        logger.warning("cache_read_error", extra={"key": key, "error": str(exc)})
        return None


async def set_cached(key: str, value: str) -> None:
    try:
        await get_redis().set(key, value, ex=CACHE_TTL)
    except Exception as exc:
        logger.warning("cache_write_error", extra={"key": key, "error": str(exc)})


async def run_with_cache(
    cache_key: str,
    execute: Callable[[], Awaitable[Any]],
    timeout: float = TASK_TIMEOUT_SECONDS,
) -> Tuple[bool, Any]:
    """Execute `execute` with Redis-backed stampede protection.

    Returns (cache_hit, result) where result is a string on cache hit
    and the raw return value of execute() on a miss.
    """
    # Fast path — result already cached
    cached = await get_cached(cache_key)
    if cached is not None:
        return True, cached

    lock_key = f"lock:{cache_key}"

    try:
        acquired = await get_redis().set(lock_key, "1", nx=True, ex=LOCK_TTL)
    except Exception as exc:
        logger.warning("cache_lock_error", extra={"key": cache_key, "error": str(exc)})
        # Redis unavailable — fall through to direct execution
        return False, await execute()

    if acquired:
        try:
            result = await execute()
            return False, result
        finally:
            try:
                await get_redis().delete(lock_key)
            except Exception:
                pass
    else:
        # Another replica holds the lock — wait for it to populate the cache
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(LOCK_POLL)
            cached = await get_cached(cache_key)
            if cached is not None:
                return True, cached
        # Lock holder took too long (or crashed and lock expired); execute directly
        logger.warning("cache_lock_wait_timeout", extra={"key": cache_key})
        return False, await execute()
