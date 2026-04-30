# Agent Execution Service

A multi-tenant AI agent execution service with production-grade observability.

**Repository:** https://github.com/DeepanshuGautam/agent-platform

---

## Submission — Pull Requests

Each task is a PR against the previous task's branch, so the diff shows only what that task added.

| Task | PR | Base → Head | What's in the diff |
|---|---|---|---|
| Task 1 — Observability | [PR #1](https://github.com/DeepanshuGautam/agent-platform/pull/1) | `main` → `task/1-observability` | OTel tracing, Prometheus metrics, Grafana dashboard, structured logs |
| Task 2 — Diagnosis | [PR #2](https://github.com/DeepanshuGautam/agent-platform/pull/2) | `task/1-observability` → `task/2-diagnosis` | `DIAGNOSIS.md` — 8 issues with trace IDs, metric values, screenshots |
| Task 3 — Fixes | [PR #3](https://github.com/DeepanshuGautam/agent-platform/pull/3) | `task/2-diagnosis` → `task/3-fixes` | 6 bug fixes; before/after load test data and screenshots |
| Task 4 — Production | [PR #4](https://github.com/DeepanshuGautam/agent-platform/pull/4) | `task/3-fixes` → `task/4-production` | Redis distributed cache, `PRODUCTION.md` |

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
        │     ├── tool:search        (concurrent)
        │     ├── tool:database_lookup (concurrent)
        │     └── tool:calculator    (concurrent)
        └── llm_summarise
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

# P95 task latency by priority
histogram_quantile(0.95, sum by (priority, le) (rate(task_duration_seconds_bucket[10m])))

# Queue wait P95 by tenant
histogram_quantile(0.95, sum by (tenant_id, le) (rate(task_queue_wait_seconds_bucket[10m])))

# Pipeline stage P95 by stage
histogram_quantile(0.95, sum by (stage, le) (rate(pipeline_stage_duration_seconds_bucket[10m])))

# Tool latency P95 by tool
histogram_quantile(0.95, sum by (tool, le) (rate(tool_execution_duration_seconds_bucket[10m])))

# LLM retry count by reason
llm_retries_total

# Token consumption by tenant
llm_tokens_total

# Cache hit rate
rate(cache_hits_total[5m]) / rate(task_requests_total[5m])
```

> **Note:** `histogram_quantile` requires the `le` label — always include it in `sum by`. Use a `[10m]` window immediately after the ~3-minute load test; a `[5m]` window will produce NaN once the data ages out.

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
| `orchestrator.py` | Per-stage duration (`plan`, `execute_tools`, `summarise`), token counts per stage |
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
| Diagnosis | Claude analyzed load test output and Prometheus metric values to identify and document each issue with evidence |
| Fixes (Task 3) | Claude proposed and implemented fixes for Issues 1, 3, 4, 6, 7, 8; re-ran load test; validated before/after numbers |
| Production doc (Task 4) | Claude drafted PRODUCTION.md using the actual metric names from the instrumentation |
| README | Claude drafted and updated this document incrementally |

### What worked well

- Using Claude to read and explain the codebase first meant instrumentation was targeted — e.g. knowing the locking order in `main.py` before deciding where to measure queue wait time.
- Running the load test inside the conversation and piping output directly to Claude let it compute before/after comparisons from real numbers rather than estimates.

### What required human oversight

- **Package version conflict:** `opentelemetry-instrumentation-fastapi` imports `pkg_resources` from `setuptools`, which was removed in setuptools 82. Claude caught the error from the Docker build log and switched to a manual `BaseHTTPMiddleware` instead.
- **429 backoff overtuning:** Claude's first fix used a 4× delay multiplier for 429 retries, which caused a latency regression. The regression was caught from load test output; Claude diagnosed the root cause and reduced the multiplier to 2×, eliminating all timeouts.
- **Tenant lock removal decision:** Claude removed the tenant lock entirely rather than swapping order. Justification was verified by code inspection: `run_task` has no per-tenant shared state, making the lock a no-op protection.
- **Cache stampede — incomplete initial analysis:** Claude initially assessed the cache stampede as "benign" (correct results, just redundant work) and documented the Redis fix as a future recommendation rather than implementing it. Human review pushed back: in production, LLM calls are billed per token and multiple replicas each carry their own isolated in-memory cache, making the stampede a real cost issue. Claude was directed to implement the fix — the Redis `SET NX` distributed lock in `src/cache.py` is the result.
- **Redis lock poll deadline bug:** The Task 4 Redis cache introduced a timeout accounting error: the waiter's poll deadline is set to `now + 30s` from when polling starts, so a waiter that exhausts the poll window and falls through to execute directly can spend up to 90s total (30s poll + 30s semaphore wait + 30s task execution). This caused 10 failures in the first Task 4 load test run, worse than the pre-fix baseline of 5. After-fix screenshots were re-taken on task/3-fixes code to isolate the Task 3 improvement. The deadline accounting bug is documented as a known limitation in PRODUCTION.md and DIAGNOSIS.md with the recommended fix.

### AI accuracy

Three outputs required correction:
1. **429 backoff multiplier (4×):** Caused a latency regression — additional timeouts observed vs. baseline. Caught via load test data and corrected to 2×.
2. **Cache stampede severity:** Initially called "benign" and left as a documentation recommendation. Human review identified this as a real production cost risk and directed the implementation. The Redis-backed cache with distributed lock is the corrected output.
3. **Redis lock poll deadline:** The Task 4 cache implementation used `now + 30s` as the poll deadline without accounting for time already spent. Caught from the Task 4 load test result (10 failures vs. 5 baseline). Documented as a known limitation with fix recommendation rather than silently working around it.

---

## Diagnosis Report

See [DIAGNOSIS.md](DIAGNOSIS.md) — 8 issues identified with real trace IDs, metric values, and log excerpts. Includes before/after comparison for all 6 fixes applied in Task 3.

## Task 3: Fixes Applied

6 issues fixed. Before/after load test results (same 100-request, concurrency-15 parameters):

| Metric | Before | After |
|---|---|---|
| Failed tasks | 5 | **0** |
| P50 latency | 15.98s | **6.91s** |
| P95 latency | 30.01s | **14.80s** |
| Max latency | 30.01s | **17.14s** |

**Changes:**
1. **Removed tenant lock** (`src/main.py`) — serialized all per-tenant traffic to protect state that doesn't exist; removing it dropped P50 by 57%
2. **Parallel tool execution** (`src/tool_executor.py`) — `asyncio.gather` replaces sequential loop
3. **Removed unused validate LLM call** (`src/orchestrator.py`) — saves ~33% of LLM calls per task
4. **Differentiated 429 retry backoff** (`src/llm_client.py`) — rate-limit errors wait 2× longer than server errors
5. **Sanitized exception handler** (`src/orchestrator.py`) — removed stack trace and LLM URL from API responses; returns `trace_id` reference instead
6. **Fixed dead summarise failure path** (`src/orchestrator.py`) — `is None` → `not ...` so summarise errors now surface as `status=failed` and emit metrics
