# Agent Execution Service

A multi-tenant AI agent execution service with production-grade observability.

---

## Running the System

```bash
docker compose up --build
```

Services that start:

| Service | URL | Purpose |
|---|---|---|
| Agent API | http://localhost:8080 | Main service |
| Mock LLM | http://localhost:8081 | Simulated LLM backend |
| Jaeger UI | http://localhost:16686 | Distributed traces |
| Prometheus | http://localhost:9090 | Metrics storage |
| Grafana | http://localhost:3000 | Dashboards (auto-provisioned, no login) |

---

## Running the Load Test

With the stack running:

```bash
python3 -m tests.test_load
```

Default: 100 requests at concurrency 15, across 3 tenants and 3 priority levels.

---

## Viewing Observability Data

### Traces (Jaeger)
1. Open http://localhost:16686
2. Select service `agent-service`
3. Search by operation (`run_task`, `llm_plan`, `tool:search`, etc.) or filter by duration/error

Each trace covers the full request lifecycle:
```
POST /tasks
  └── run_task
        ├── llm_plan
        ├── execute_tools
        │     ├── tool:search
        │     ├── tool:database_lookup
        │     └── tool:calculator
        ├── llm_summarise
        └── llm_validate
```

### Grafana Dashboard
Open http://localhost:3000 — the **Agent Execution Service** dashboard loads automatically (no login required). It shows task throughput, latency percentiles, error rate, cache hit rate, queue wait by tenant, LLM retry reasons, tool latency, and token consumption.

Prometheus and Jaeger are pre-configured as data sources — no manual setup needed.

### Metrics (Prometheus)
Raw metrics: http://localhost:8080/metrics

Key metrics to query in Prometheus (http://localhost:9090):

```promql
# Request rate by tenant and status
rate(task_requests_total[1m])

# P95 task latency
histogram_quantile(0.95, rate(task_duration_seconds_bucket[5m]))

# Queue wait time by tenant
histogram_quantile(0.95, rate(task_queue_wait_seconds_bucket[5m]))

# LLM retry rate by reason
rate(llm_retries_total[1m])

# Token consumption by tenant
rate(llm_tokens_total[5m])

# Cache hit rate
rate(cache_hits_total[5m]) / rate(task_requests_total[5m])
```

### Logs
Logs are emitted as structured JSON with `trace_id` and `span_id` on every line. To correlate a log entry to a Jaeger trace, copy the `trace_id` from a log line and paste it into the Jaeger trace search.

```bash
docker compose logs -f agent-service
```

---

## Observability Design

### Stack
- **OpenTelemetry** (SDK + OTLP HTTP exporter) for traces — vendor-neutral, works with any compatible backend
- **Jaeger all-in-one** for trace collection and visualization
- **Prometheus** for metrics scraping and storage
- **Grafana** for dashboards
- **python-json-logger** for structured JSON logs with automatic trace/span ID injection

Auto-instrumentation packages (`opentelemetry-instrumentation-fastapi`) were dropped — they import `pkg_resources` which was removed in setuptools 82. Replaced with a manual `BaseHTTPMiddleware` in `src/telemetry.py`, which is simpler and avoids the dependency.

### Instrumentation points

| Layer | What's measured |
|---|---|
| `main.py` | End-to-end task duration, queue wait time, cache hits, active task count, token usage per tenant |
| `orchestrator.py` | Per-stage duration (`plan`, `execute_tools`, `summarise`, `validate`), token counts per stage |
| `llm_client.py` | Per-attempt latency, outcome (success / 429 / 500 / timeout), retry counts by reason |
| `tool_executor.py` | Per-tool latency (`search`, `database_lookup`, `calculator`) |

All metrics are labeled by tenant, priority, and/or status to allow slicing by dimensions that matter for multi-tenant diagnosis.

---

## AI Tool Usage

### Tools used

**Claude Code (claude-sonnet-4-6)** — used throughout this challenge as the primary engineering assistant.

### Tasks performed with AI assistance

| Task | How AI was used |
|---|---|
| Codebase understanding | Asked Claude to explain each file and the request flow before touching anything |
| Observability design | Discussed stack choice (OTel + Jaeger + Prometheus + Grafana) and span hierarchy before implementing |
| Instrumentation | Claude generated all instrumentation code across `telemetry.py`, `main.py`, `orchestrator.py`, `llm_client.py`, `tool_executor.py` |
| README | Claude drafted this document |

### What worked well

- Using Claude to read and explain the codebase first, before writing any code, meant the instrumentation was targeted rather than generic — e.g. knowing the locking order in `main.py` before deciding where to measure queue wait time.
- Asking Claude to explain concepts (async, locks, semaphores) in plain terms helped validate understanding of the code before diagnosing issues.

### What required human oversight

- **Package version conflict:** `opentelemetry-instrumentation-fastapi` imports `pkg_resources` from `setuptools`, which was removed in setuptools 82. Claude caught the error from the Docker build log and switched to a manual `BaseHTTPMiddleware` instead of auto-instrumentation.
- Claude does not run the code, so all instrumentation was reviewed before `docker compose up` to catch import errors or misconfigured metric labels.

### AI accuracy

No factually incorrect outputs were identified during instrumentation. All generated code was reviewed for correctness against the existing codebase structure before being accepted.
