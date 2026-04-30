"""FastAPI application — Agent Execution Service.

Provides the HTTP API for submitting and querying agent tasks.
"""

import uuid
import time
import logging
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task
from src.config import MAX_CONCURRENT_TASKS, TASK_TIMEOUT_SECONDS
from src.telemetry import (
    setup_telemetry,
    TASK_REQUESTS, TASK_DURATION, TASK_QUEUE_WAIT,
    CACHE_HITS, TOKEN_USAGE, ACTIVE_TASKS,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Execution Service")
setup_telemetry(app)

# Task storage
task_store: dict[str, TaskResult] = {}

# Response cache for repeated queries — avoids redundant LLM calls
_response_cache: dict[str, dict] = {}

# Limit concurrent task executions to protect downstream LLM service
_task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Ensure tasks for the same tenant execute in submission order
# to prevent race conditions on downstream tenant state
_tenant_locks: dict[str, asyncio.Lock] = {}


class CreateTaskBody(BaseModel):
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody):
    task_id = str(uuid.uuid4())
    t_submitted = time.time()

    logger.info("task_received", extra={
        "task_id": task_id,
        "tenant_id": body.tenant_id,
        "priority": body.priority.value,
    })

    # Cache key: tenant + description (priority excluded because
    # task results are priority-independent in the current design)
    cache_key = f"{body.tenant_id}:{body.task_description}"
    if cache_key in _response_cache:
        CACHE_HITS.labels(tenant_id=body.tenant_id).inc()
        logger.info("cache_hit", extra={"task_id": task_id, "tenant_id": body.tenant_id})
        cached = _response_cache[cache_key]
        result = TaskResult(
            task_id=task_id, status=TaskStatus.COMPLETED,
            tenant_id=body.tenant_id, priority=body.priority,
            result=cached.get("result"),
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            created_at=time.time(), completed_at=time.time(),
        )
        task_store[task_id] = result
        TASK_REQUESTS.labels(
            tenant_id=body.tenant_id, priority=body.priority.value, status="completed",
        ).inc()
        TASK_DURATION.labels(
            tenant_id=body.tenant_id, priority=body.priority.value, status="completed",
        ).observe(time.time() - t_submitted)
        return _to_response(result)

    # Execute the task (bounded by concurrency limit)
    task_store[task_id] = TaskResult(
        task_id=task_id, status=TaskStatus.PENDING,
        tenant_id=body.tenant_id, priority=body.priority,
    )

    async def _guarded_execute():
        lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())
        async with lock:
            async with _task_semaphore:
                queue_wait = time.time() - t_submitted
                TASK_QUEUE_WAIT.labels(tenant_id=body.tenant_id).observe(queue_wait)
                logger.info("task_executing", extra={
                    "task_id": task_id,
                    "tenant_id": body.tenant_id,
                    "queue_wait_seconds": round(queue_wait, 3),
                })
                ACTIVE_TASKS.inc()
                try:
                    return await run_task(
                        task_id=task_id,
                        description=body.task_description,
                        tenant_id=body.tenant_id,
                        priority=body.priority,
                    )
                finally:
                    ACTIVE_TASKS.dec()

    # Enforce task-level deadline: clients should not wait indefinitely
    try:
        result = await asyncio.wait_for(
            _guarded_execute(), timeout=TASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("task_timeout", extra={
            "task_id": task_id,
            "tenant_id": body.tenant_id,
            "timeout_seconds": TASK_TIMEOUT_SECONDS,
        })
        result = TaskResult(
            task_id=task_id, status=TaskStatus.FAILED,
            tenant_id=body.tenant_id, priority=body.priority,
            error="Task execution exceeded time limit",
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            created_at=time.time(), completed_at=time.time(),
        )

    task_store[task_id] = result
    duration = time.time() - t_submitted
    status_label = result.status.value

    TASK_REQUESTS.labels(
        tenant_id=body.tenant_id, priority=body.priority.value, status=status_label,
    ).inc()
    TASK_DURATION.labels(
        tenant_id=body.tenant_id, priority=body.priority.value, status=status_label,
    ).observe(duration)

    if result.token_usage:
        TOKEN_USAGE.labels(tenant_id=body.tenant_id, token_type="prompt").inc(
            result.token_usage.get("prompt_tokens", 0)
        )
        TOKEN_USAGE.labels(tenant_id=body.tenant_id, token_type="completion").inc(
            result.token_usage.get("completion_tokens", 0)
        )

    logger.info("task_completed", extra={
        "task_id": task_id,
        "tenant_id": body.tenant_id,
        "status": status_label,
        "duration_seconds": round(duration, 3),
        "token_usage": result.token_usage,
    })

    # Cache successful responses for future identical requests
    if result.status == TaskStatus.COMPLETED:
        _response_cache[cache_key] = {"result": result.result}

    return _to_response(result)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    if task_id not in task_store:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task_store[task_id])


@app.get("/metrics")
async def metrics_endpoint():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    return {"status": "ok"}


def _to_response(r: TaskResult) -> TaskResponse:
    return TaskResponse(
        task_id=r.task_id, status=r.status,
        tenant_id=r.tenant_id, priority=r.priority,
        result=r.result, error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at, completed_at=r.completed_at,
    )
