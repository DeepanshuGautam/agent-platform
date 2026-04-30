# Production Readiness — Agent Execution Service

## SLIs and SLOs

| SLI | Definition | SLO |
|---|---|---|
| Task success rate | `1 - rate(task_requests_total{status="failed"}[5m]) / rate(task_requests_total[5m])` | ≥ 99.5% over 30 days |
| P95 task latency | `histogram_quantile(0.95, rate(task_duration_seconds_bucket[5m]))` | ≤ 20s |
| P95 queue wait | `histogram_quantile(0.95, rate(task_queue_wait_seconds_bucket[5m]))` | ≤ 5s |
| Cache hit rate | `rate(cache_hits_total[5m]) / rate(task_requests_total[5m])` | ≥ 30% under repeated load |

---

## Alerting

```yaml
groups:
  - name: agent-service
    rules:

      - alert: HighErrorRate
        expr: |
          rate(task_requests_total{status="failed"}[5m])
          / rate(task_requests_total[5m]) > 0.01
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Error rate > 1% for 2 minutes"

      - alert: CriticalErrorRate
        expr: |
          rate(task_requests_total{status="failed"}[5m])
          / rate(task_requests_total[5m]) > 0.05
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Error rate > 5% — page on-call"

      - alert: HighP95Latency
        expr: |
          histogram_quantile(0.95,
            rate(task_duration_seconds_bucket[5m])) > 20
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P95 task latency > 20s"

      - alert: LLMRetryStorm
        expr: rate(llm_retries_total[1m]) > 5
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "LLM retry rate elevated — check upstream availability"

      - alert: QueueBuildup
        expr: |
          histogram_quantile(0.95,
            rate(task_queue_wait_seconds_bucket[5m])) > 10
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "Tasks waiting > 10s for a semaphore slot — consider scaling"

      - alert: ErrorBudgetBurnRate
        expr: |
          (
            rate(task_requests_total{status="failed"}[1h])
            / rate(task_requests_total[1h])
          ) / (1 - 0.995) > 14.4
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Error budget burning at > 14.4× — 1-hour burn exceeds 1-day budget"
```

---

## Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: agent-service
  template:
    metadata:
      labels:
        app: agent-service
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: agent-service
          image: agent-service:latest
          ports:
            - containerPort: 8080
          env:
            - name: LLM_SERVER_URL
              valueFrom:
                secretKeyRef:
                  name: agent-secrets
                  key: llm-server-url
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: agent-secrets
                  key: redis-url
            - name: OTLP_ENDPOINT
              value: "http://jaeger-collector:4318/v1/traces"
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "1000m"
              memory: "512Mi"
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 15
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 2
```

---

## Scaling

**Horizontal scaling is safe.** The service is stateless in all paths that matter:

- Task results are stored in `task_store` (in-process dict) only for the duration of a request. GET `/tasks/{id}` is for immediate polling; there is no requirement for cross-replica task lookup in the current design.
- The response cache moved from an in-process dict to Redis (`src/cache.py`). All replicas share the same cache and the same distributed lock, so a cache hit on any replica avoids LLM calls across the cluster.
- The semaphore (`_task_semaphore`) is still per-process. With N replicas, effective concurrency is `N × MAX_CONCURRENT_TASKS`. Tune `MAX_CONCURRENT_TASKS` down as replicas increase to keep total LLM load constant.

**Vertical scaling is bounded.** Each task holds a semaphore slot and an active asyncio coroutine stack. Memory and LLM upstream capacity (not CPU) are the binding constraints.

---

## Storage

| Component | What's stored | Production recommendation |
|---|---|---|
| `task_store` (in-process dict) | In-flight task results for current requests | No change needed — ephemeral by design |
| Response cache | Completed task results keyed by `tenant_id:description` | Redis (implemented in `src/cache.py`); use a managed Redis (ElastiCache / Cloud Memorystore) with persistence disabled and an eviction policy of `allkeys-lru` |
| Execution audit log (`_execution_log` in `orchestrator.py`) | Full prompt/response pairs per task | Ship to an append-only store (S3, BigQuery) — the in-process list grows unbounded and is lost on restart |

---

## Cache Stampede Protection

With multiple replicas, an in-process dict cache is ineffective: each replica caches independently, so identical requests hitting different replicas all execute the full LLM pipeline. This is a direct cost issue — LLM calls are billed per token.

**Implementation (`src/cache.py`):** Redis-backed cache with a distributed `SET NX` lock:

1. **Check** — `GET cache_key` — return immediately on hit
2. **Lock** — `SET lock_key "1" NX EX 60` — only one replica proceeds
3. **Re-check** — winner re-reads cache after acquiring (another replica may have just written)
4. **Execute** — run the full LLM pipeline
5. **Write** — `SET cache_key result EX 3600`
6. **Unlock** — `DEL lock_key` (in `finally` — safe even on exception)

Waiters (replicas that lost the lock race) poll the cache every 250ms. If the lock holder crashes, the `EX 60` TTL auto-expires the lock so waiters eventually fall through and execute directly. Redis unavailability degrades gracefully to direct execution with a warning log.

**Known limitation — poll deadline accounting:** The waiter's poll deadline is set to `now + TASK_TIMEOUT_SECONDS` (30s) from when polling begins, regardless of how much of the task's budget has already been consumed. If the poll window expires and the waiter falls through to execute directly, it can then spend another 30s waiting for the global semaphore plus 30s running the task. The combined worst-case path for a lock-waiter reaches ~90s, which exceeds the intended 30s task budget and can cause task failures under high concurrency. The fix is to pass the absolute task deadline (submission time + TASK_TIMEOUT_SECONDS) into `run_with_cache` and derive the remaining poll window from it, so the lock poll plus execution never exceed the original budget.

---

## Runbook Excerpts

### High error rate

1. Check `rate(llm_retries_total{reason="rate_limit"}[1m])` — if elevated, the LLM upstream is throttling. Reduce `MAX_CONCURRENT_TASKS` or add request-level queuing.
2. Check `rate(llm_retries_total{reason="server_error"}[1m])` — if elevated, the LLM upstream is unhealthy. Check upstream status page.
3. Pull a failing trace from Jaeger: filter by `task.error` tag or `status=error`. The `error` field in the API response contains a `trace_id` reference for direct lookup.

### High queue wait time

1. Check `active_tasks` gauge — if pinned at `MAX_CONCURRENT_TASKS`, semaphore is saturated.
2. Check P95 `tool_duration_seconds` — if one tool (typically `database_lookup`) is slow, it's holding slots longer than expected.
3. Scale replicas or increase `MAX_CONCURRENT_TASKS` (with care — each slot generates upstream LLM load).

### LLM retry storm

1. Check `llm_retries_total` labeled by `reason`. `rate_limit` means slow down; `server_error` means the LLM is degraded; `timeout` means network or load issue.
2. The current retry policy uses exponential backoff: base 0.5s, factor 2×, max 5 attempts, with 429s using a 2× additional delay multiplier to avoid compounding rate-limit pressure.
