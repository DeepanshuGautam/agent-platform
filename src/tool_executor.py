"""Simulated tool execution layer.

Each tool simulates an external service call (search engine,
database, calculator, etc.) with realistic latency.
"""

import asyncio
import random
import logging
import time
from src.telemetry import tracer, TOOL_DURATION

logger = logging.getLogger(__name__)


async def execute_tool(tool_name: str, args: dict) -> dict:
    """Execute a single tool and return its result."""
    # Simulate variable latency per tool type
    latency_map = {
        "search": (0.1, 0.5),
        "database_lookup": (0.05, 0.2),
        "calculator": (0.01, 0.05),
    }
    low, high = latency_map.get(tool_name, (0.05, 0.3))

    with tracer.start_as_current_span(f"tool:{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.args", str(args)[:200])
        t0 = time.time()
        await asyncio.sleep(random.uniform(low, high))
        elapsed = time.time() - t0
        TOOL_DURATION.labels(tool_name=tool_name).observe(elapsed)
        logger.debug("tool_executed", extra={
            "tool_name": tool_name,
            "elapsed_seconds": round(elapsed, 3),
        })
        result = {
            "tool": tool_name,
            "status": "success",
            "output": f"Result from {tool_name}",
        }
        span.set_attribute("tool.status", "success")
        return result


async def execute_tools(tools: list[tuple[str, dict]]) -> list[dict]:
    """Execute multiple tools and return results in order.

    Args:
        tools: List of (tool_name, args) tuples to execute.

    Returns:
        Ordered list of tool execution results.
    """
    results = []
    for tool_name, args in tools:
        result = await execute_tool(tool_name, args)
        results.append(result)
    return results
