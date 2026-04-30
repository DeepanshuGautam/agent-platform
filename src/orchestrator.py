"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
"""

import time
import logging
from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.telemetry import tracer, PIPELINE_STAGE_DURATION

logger = logging.getLogger(__name__)

# Execution audit trail for debugging and compliance review
_execution_log: list[dict] = []


async def run_task(task_id: str, description: str,
                   tenant_id: str, priority: Priority) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise pipeline."""
    created = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0

    with tracer.start_as_current_span("run_task") as span:
        span.set_attribute("task.id", task_id)
        span.set_attribute("task.tenant_id", tenant_id)
        span.set_attribute("task.priority", priority.value)
        span.set_attribute("task.description", description[:200])

        try:
            # ── Step 1: Planning ──────────────────────────────────
            t0 = time.time()
            with tracer.start_as_current_span("llm_plan") as stage_span:
                plan = await call_llm(
                    prompt=f"Plan the following task: {description}",
                    max_tokens=256,
                )
                stage_span.set_attribute("llm.prompt_tokens", plan.get("prompt_tokens", 0))
                stage_span.set_attribute("llm.completion_tokens", plan.get("completion_tokens", 0))
                stage_span.set_attribute("llm.error", bool(plan.get("error")))

            stage_status = "error" if plan.get("error") else "ok"
            PIPELINE_STAGE_DURATION.labels(stage="plan", status=stage_status).observe(time.time() - t0)
            total_prompt_tokens += plan.get("prompt_tokens", 0)
            total_completion_tokens += plan.get("completion_tokens", 0)

            logger.info("pipeline_stage_complete", extra={
                "task_id": task_id, "stage": "plan", "status": stage_status,
                "duration_seconds": round(time.time() - t0, 3),
            })

            if plan.get("error"):
                span.set_attribute("task.failed_stage", "plan")
                return TaskResult(
                    task_id=task_id, status=TaskStatus.FAILED,
                    tenant_id=tenant_id, priority=priority,
                    error=plan["error"],
                    token_usage={"prompt_tokens": total_prompt_tokens,
                                 "completion_tokens": total_completion_tokens},
                    created_at=created, completed_at=time.time(),
                )

            # ── Step 2: Tool execution ───────────────────────────
            t0 = time.time()
            tools_to_run = [
                ("search", {"query": description}),
                ("database_lookup", {"key": tenant_id}),
                ("calculator", {"expression": "1+1"}),
            ]
            with tracer.start_as_current_span("execute_tools"):
                tool_results = await execute_tools(tools_to_run)

            PIPELINE_STAGE_DURATION.labels(stage="execute_tools", status="ok").observe(time.time() - t0)
            logger.info("pipeline_stage_complete", extra={
                "task_id": task_id, "stage": "execute_tools",
                "tool_count": len(tool_results),
                "duration_seconds": round(time.time() - t0, 3),
            })

            # ── Step 3: Summarise ────────────────────────────────
            t0 = time.time()
            summary_prompt = (
                f"Summarise results for task: {description}\n"
                f"Tool outputs: {tool_results}"
            )
            with tracer.start_as_current_span("llm_summarise") as stage_span:
                summary = await call_llm(prompt=summary_prompt, max_tokens=512)
                stage_span.set_attribute("llm.prompt_tokens", summary.get("prompt_tokens", 0))
                stage_span.set_attribute("llm.completion_tokens", summary.get("completion_tokens", 0))
                stage_span.set_attribute("llm.error", bool(summary.get("error")))

            stage_status = "error" if summary.get("error") and not summary.get("text") else "ok"
            PIPELINE_STAGE_DURATION.labels(stage="summarise", status=stage_status).observe(time.time() - t0)
            total_prompt_tokens += summary.get("prompt_tokens", 0)
            total_completion_tokens += summary.get("completion_tokens", 0)

            logger.info("pipeline_stage_complete", extra={
                "task_id": task_id, "stage": "summarise", "status": stage_status,
                "duration_seconds": round(time.time() - t0, 3),
            })

            if summary.get("error") and not summary.get("text"):
                span.set_attribute("task.failed_stage", "summarise")
                return TaskResult(
                    task_id=task_id, status=TaskStatus.FAILED,
                    tenant_id=tenant_id, priority=priority,
                    error=summary["error"],
                    token_usage={"prompt_tokens": total_prompt_tokens,
                                 "completion_tokens": total_completion_tokens},
                    created_at=created, completed_at=time.time(),
                )

            # Record execution details for audit trail
            _execution_log.append({
                "task_id": task_id,
                "tenant_id": tenant_id,
                "description": description,
                "plan_prompt": f"Plan the following task: {description}",
                "plan_response": plan,
                "tool_results": tool_results,
                "summary_prompt": summary_prompt,
                "summary_response": summary,
                "token_usage": {"prompt": total_prompt_tokens,
                                "completion": total_completion_tokens},
                "completed_at": time.time(),
            })

            span.set_attribute("task.total_prompt_tokens", total_prompt_tokens)
            span.set_attribute("task.total_completion_tokens", total_completion_tokens)

            return TaskResult(
                task_id=task_id, status=TaskStatus.COMPLETED,
                tenant_id=tenant_id, priority=priority,
                result=summary.get("text", ""),
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        except Exception as e:
            logger.exception("task_pipeline_error", extra={
                "task_id": task_id, "tenant_id": tenant_id, "error": str(e),
            })
            span.record_exception(e)
            span.set_attribute("task.error", str(e))
            ctx = span.get_span_context()
            trace_ref = format(ctx.trace_id, "032x") if ctx and ctx.is_valid else "unknown"
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=f"Task execution failed. Reference trace_id={trace_ref} for details.",
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )
