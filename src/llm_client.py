"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.
"""

import httpx
import asyncio
import random
import time
import logging
from src.config import (
    LLM_SERVER_URL,
    TASK_TIMEOUT_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
)
from src.telemetry import tracer, LLM_CALLS, LLM_RETRIES, LLM_DURATION

logger = logging.getLogger(__name__)

# Shared HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=TASK_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


class _TokenBucket:
    """Rate limiter to protect the downstream LLM service from overload
    and prevent runaway inference costs during traffic spikes."""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._rate)
            self._last_refill = now
            while self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._rate)
                self._last_refill = now
            self._tokens -= 1


# Global rate limiter for LLM calls
_rate_limiter = _TokenBucket(rate=LLM_RATE_LIMIT_RPS, capacity=LLM_RATE_LIMIT_BURST)


async def call_llm(prompt: str, max_tokens: int = 512) -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    client = _get_client()
    last_error = None
    last_status = None
    accumulated_tokens = 0

    with tracer.start_as_current_span("llm_call") as span:
        span.set_attribute("llm.max_tokens", max_tokens)
        span.set_attribute("llm.prompt_length", len(prompt))
        span.set_attribute("llm.max_attempts", RETRY_MAX_ATTEMPTS)

        # Unified retry policy: all transient errors (500, 429, timeout)
        # use the same exponential backoff strategy for simplicity
        for attempt in range(RETRY_MAX_ATTEMPTS):
            span.set_attribute("llm.attempt", attempt)
            t0 = time.time()
            try:
                await _rate_limiter.acquire()
                response = await client.post(
                    f"{LLM_SERVER_URL}/v1/inference",
                    json={"prompt": prompt, "max_tokens": max_tokens},
                )
                elapsed = time.time() - t0

                if response.status_code == 200:
                    data = response.json()
                    # Include any token overhead from failed attempts
                    data["prompt_tokens"] = data.get("prompt_tokens", 0) + accumulated_tokens
                    LLM_CALLS.labels(status="success").inc()
                    LLM_DURATION.labels(status="success").observe(elapsed)
                    span.set_attribute("llm.status", "success")
                    span.set_attribute("llm.final_attempt", attempt)
                    logger.debug("llm_call_success", extra={
                        "attempt": attempt,
                        "elapsed_seconds": round(elapsed, 3),
                        "prompt_tokens": data.get("prompt_tokens", 0),
                        "completion_tokens": data.get("completion_tokens", 0),
                    })
                    return data

                last_status = response.status_code
                last_error = f"LLM returned {response.status_code}"

                # Track estimated tokens for failed attempts that were
                # partially processed by the LLM before failing
                if response.status_code == 500:
                    reason = "server_error"
                    accumulated_tokens += max(1, len(prompt.split()))
                elif response.status_code == 429:
                    reason = "rate_limit"
                else:
                    reason = f"http_{response.status_code}"

                LLM_CALLS.labels(status=str(response.status_code)).inc()
                LLM_DURATION.labels(status=str(response.status_code)).observe(elapsed)
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    LLM_RETRIES.labels(reason=reason).inc()

                logger.warning("llm_call_failed", extra={
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "reason": reason,
                    "elapsed_seconds": round(elapsed, 3),
                })

            except httpx.TimeoutException:
                elapsed = time.time() - t0
                last_error = "LLM request timed out"
                last_status = 408
                LLM_CALLS.labels(status="timeout").inc()
                LLM_DURATION.labels(status="timeout").observe(elapsed)
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    LLM_RETRIES.labels(reason="timeout").inc()
                logger.warning("llm_call_timeout", extra={
                    "attempt": attempt,
                    "elapsed_seconds": round(elapsed, 3),
                })
            except Exception as e:
                elapsed = time.time() - t0
                last_error = str(e)
                last_status = 0
                LLM_CALLS.labels(status="exception").inc()
                LLM_DURATION.labels(status="exception").observe(elapsed)
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    LLM_RETRIES.labels(reason="exception").inc()
                logger.error("llm_call_exception", extra={
                    "attempt": attempt,
                    "error": str(e),
                    "elapsed_seconds": round(elapsed, 3),
                })

            # Differentiated backoff: 429 waits 2× longer than 500 to respect
            # the rate-limit signal without blowing the 30s task budget
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                if last_status == 429:
                    await asyncio.sleep(delay * 2 + random.uniform(0, 0.5))
                else:
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.3))

        span.set_attribute("llm.status", "exhausted")
        span.set_attribute("llm.last_status_code", last_status or 0)
        logger.error("llm_retries_exhausted", extra={
            "attempts": RETRY_MAX_ATTEMPTS,
            "last_error": last_error,
            "last_status": last_status,
        })

        return {
            "error": last_error,
            "text": "",
            "prompt_tokens": accumulated_tokens,
            "completion_tokens": 0,
            "status_code": last_status,
        }
