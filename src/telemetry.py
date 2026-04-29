"""Observability setup — OpenTelemetry tracing + Prometheus metrics + structured logging."""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from prometheus_client import Counter, Histogram, Gauge
from pythonjsonlogger import jsonlogger

OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://jaeger:4318/v1/traces")


def setup_telemetry(app) -> None:
    _setup_tracing(app)
    _setup_logging()


def _setup_tracing(app) -> None:
    resource = Resource.create({SERVICE_NAME: "agent-service"})
    exporter = OTLPSpanExporter(endpoint=OTLP_ENDPOINT)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    app.add_middleware(_TracingMiddleware)


class _TracingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/metrics", "/health"):
            return await call_next(request)
        span_name = f"{request.method} {request.url.path}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.url", str(request.url))
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            return response


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(_TraceContextFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class _TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = ""
            record.span_id = ""
        return True


tracer = trace.get_tracer("agent-service")

# Prometheus metrics

TASK_REQUESTS = Counter(
    "task_requests_total",
    "Total task requests by outcome",
    ["tenant_id", "priority", "status"],
)
TASK_DURATION = Histogram(
    "task_duration_seconds",
    "End-to-end task duration including queue wait",
    ["tenant_id", "priority", "status"],
    buckets=[0.5, 1, 2, 5, 10, 15, 20, 30],
)
TASK_QUEUE_WAIT = Histogram(
    "task_queue_wait_seconds",
    "Time spent waiting for tenant lock + semaphore slot",
    ["tenant_id"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 30],
)
CACHE_HITS = Counter(
    "cache_hits_total",
    "Response cache hits — LLM call skipped",
    ["tenant_id"],
)
LLM_CALLS = Counter(
    "llm_calls_total",
    "LLM API call outcomes per attempt",
    ["status"],
)
LLM_RETRIES = Counter(
    "llm_retries_total",
    "LLM retry attempts by failure reason",
    ["reason"],
)
LLM_DURATION = Histogram(
    "llm_call_duration_seconds",
    "LLM call latency per attempt",
    ["status"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 15],
)
TOOL_DURATION = Histogram(
    "tool_execution_duration_seconds",
    "Individual tool execution latency",
    ["tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1],
)
PIPELINE_STAGE_DURATION = Histogram(
    "pipeline_stage_duration_seconds",
    "Duration of each orchestrator pipeline stage",
    ["stage", "status"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 15],
)
TOKEN_USAGE = Counter(
    "llm_tokens_total",
    "LLM tokens consumed",
    ["tenant_id", "token_type"],
)
ACTIVE_TASKS = Gauge(
    "active_tasks_count",
    "Tasks currently executing past the queue",
)
