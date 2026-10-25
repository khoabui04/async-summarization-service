# Design Notes — Async Summarization Service

## Architecture and trade-offs

The FastAPI server creates a job in SQLite and returns HTTP 202. A separate Python worker claims eligible jobs and calls an interchangeable provider implementation. Users poll `GET /jobs/{id}`. SQLite serves as both durable job store and minimal queue, avoiding Redis/Celery overhead in a 4–5 hour assessment. `POST /jobs` accepts an optional idempotency key; SQLite's unique index and transaction prevent duplicate submissions with that key.

The simulated provider waits 1–3 seconds, rejects invalid input, models 15% temporary failures and a two-call concurrency limit within a provider instance. The SQLite-backed worker claim limit independently restricts the normal worker path to two concurrent jobs across workers sharing the same database. It is deterministic per `(SEED, job_id, attempt)` and emits cheap, deliberately trivial summaries. The provider interface makes fast, targeted tests possible.

## Error behavior and in-flight work

Provider 400 and other non-429 4xx are classified as terminal (`failed`); provider 500, 429 and unexpected errors are retryable. Each retryable failure is returned to `queued` with a scheduled next-run time. Backoff increases exponentially; a 429 respects the greater of that backoff or `Retry-After`. After five failed attempts the job transitions to `dead`, retaining the last error and attempt count.

An atomic SQLite `BEGIN IMMEDIATE` claim prevents simultaneous ownership and caps running jobs at two across workers sharing the database. Graceful SIGTERM/SIGINT makes a worker finish its current request before it exits. Sudden crashes are handled by a 30-second lease: an expired `running` job returns to the queue. A late previous owner cannot overwrite the replacement worker's result.

## Delivery semantics and poison jobs

The service is **at-least-once**, not exactly-once, regarding external provider calls. A crashed worker may already have spent tokens before the replacement retries; without provider-level idempotency there is no guarantee against duplicate external billing. The database prevents two workers from owning the same job simultaneously under a valid lease, but that is not exactly-once invocation.

Poison jobs stop after five tries and become `dead` rather than retry forever, so they do not monopolize workers. An operator could later inspect/requeue dead jobs, but no admin requeue endpoint is included.

## Noisy tenants, scaling, and fairness

A client posting 10,000 jobs fills the SQLite queue. For the supplied SQLite-backed worker path, the database claim limit restricts active jobs to two, reducing overload of the simulated provider; this is not a general distributed rate limiter for arbitrary external callers. A single busy submitter may still crowd out other clients. There is no authentication, per-tenant identification, queue capacity limit, or fair scheduling in this assessment. A production design would add tenant-aware quotas, fair scheduling (e.g. weighted per-tenant queues), admission control, and a bounded backlog with HTTP 429 when necessary.

At ~100x load the first bottlenecks are SQLite's serialized write transactions and the provider's two-call throughput. The first changes would be a real durable queue (e.g. SQS/RabbitMQ/Redis Streams), Postgres for job metadata, shared distributed concurrency limits, bounded worker pools, and independent API/worker autoscaling. Add lease heartbeats for providers with unbounded request latency.

## Spend accounting and safeguards

Success token usage uses the provider's simulated token formula and specified price schedule; unsuccessful provider attempts do not report token counts, so the demo records zero cost for those attempts. For real billing, persist usage at **attempt level** and reconcile charges, including duplicate calls and possible billed errors. Guardrails should include input token caps, per-tenant budgets, global daily spend caps, and dashboards showing attempt rate, errors, p95 latency, queue depth, token usage and projected spend.

## Next work and limitations

Implement safe callback dispatch with destination validation (SSRF prevention) and signed/retried delivery. Add migration tooling, richer metrics/alerts, jitter on backoff, an administrative dead-letter requeue, lease renewal, and load tests. Strengthen race-condition integration tests and test Docker Compose in CI. On Windows, the project was manually verified using `docker compose up --build`: API and worker containers reached `Up`, `/healthz` responded, and a Docker-processed summarization job reached `succeeded`. The existing 10 pytest tests also passed under Python 3.14.5; these checks do not replace CI or load testing. The service deliberately excludes frontend, login/accounts, cloud provisioning, real LLM prompts and summary-quality evaluation, as suggested by the assessment.
