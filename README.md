# Tech Assessment 01 — Async Summarization Service

A small, runnable asynchronous text summarization service. Built with **Python, FastAPI, SQLite**, and a **separate worker process**. The model provider is deliberately simulated per the assessment; **no API key or real LLM is required**.

## Quick start — Docker (recommended)

Prerequisite: Docker Desktop (or Docker Engine) with Docker Compose; the Docker Engine must be running.

```bash
docker compose up --build
```

API: http://localhost:8000 ; interactive API docs: http://localhost:8000/docs

Run tests on your host using the local instructions below (or `docker compose run --rm api pytest` only if your Docker image includes tests; the provided lightweight image intentionally does not).

Stop containers with `Ctrl+C`; restart using `docker compose up`. To inspect the running services, use `docker compose ps`. The named volume `job_data` preserves jobs across restarts. **Do not run `docker compose down -v` if you want to retain data.**

## Quick start — Local Python

Prerequisite: Python 3.10+ (3.12 recommended). Open a terminal in the repository root. The service was also run locally and tested successfully with Python 3.14.5 on Windows; invoke `py -3.14 -m ...` instead of `python -m ...` if your `python` command selects an older interpreter.

```bash
python -m venv .venv
```

Activate the environment:

- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- macOS/Linux: `source .venv/bin/activate`

Then:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**In a second terminal**, from the same project folder and with the virtual environment activated:

```bash
python -m app.worker
```

The API and worker share `data/jobs.db` (created automatically). Do not launch them from different working directories unless you set an identical absolute `DB_PATH` for both.

## API examples

Create a job (returns HTTP 202 immediately):

```bash
curl -i -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{"text":"This is a fairly long text. It contains some other content. It will be summarized."}'
```

Response example (ID changes each time):

```json
{"job_id":"b8e9f2a0-...","status":"queued"}
```

Poll with the actual job ID:

```bash
curl http://localhost:8000/jobs/<YOUR_JOB_ID>
curl http://localhost:8000/healthz
curl http://localhost:8000/metrics
```

Response includes `id`, `status`, `summary`, `error`, `attempts`, `input_tokens`, `output_tokens`, `cost`, `created_at`, and `updated_at`.

API documentation: http://localhost:8000/docs (the easiest way to try it on Windows).

For a duplicate submission, reuse the `Idempotency-Key` header with the **same payload**; it returns the same job ID. A different payload with the same key gives HTTP 409. Header is optional.

`callback_url` is accepted and saved as optional metadata but **not invoked** (see limitations below). Users should poll `GET /jobs/{id}`.

## Run tests

```bash
python -m pytest -q
```

Verified on Windows with Python 3.14.5: **10 passed, 1 warning** (`StarletteDeprecationWarning` from the installed FastAPI/Starlette test client dependency). This warning did not fail the tests.

10 tests cover: success and costs, invalid input, 500 retry, 429 Retry-After, exhausted retries, idempotency/conflict, persistence across app restart, expired-lease reclamation, multi-worker claim safety, deterministic provider and token counts.

## Provider simulation

Implemented in `app/provider.py` behind a `SummarizationProvider` Protocol, allowing test fakes and replacement.

- Each valid call sleeps uniformly from **1–3 seconds**.
- Approximately **15%** of valid calls fail with provider HTTP **500**.
- Simulated provider instance admits at most **2 concurrent calls**; excess calls raise **429**, with Retry-After **1–3 seconds**. A database-backed global claim limit additionally caps workers to **2 active jobs** so the normal worker path usually avoids 429.
- Empty/whitespace-only input or input longer than **10,000 characters** produces **400 invalid_input**. Input is intentionally validated at the provider layer so a submitted job can transition to `failed`.
- Success returns trivial summary (up to first two sentences), `ceil(len(text)/4)` input tokens, `ceil(input_tokens*0.2)` output tokens.
- Rates: **$0.003 / 1000 input tokens**, **$0.015 / 1000 output tokens**.
- `SEED` (default `42`) seeds reproducible behavior derived from `(SEED, job_id, attempt)`; repeat requests using the same job ID, attempt and seed have identical simulated outcomes. Separate newly created jobs have different IDs and thus different behavior.

## Worker retries and job states

`queued -> running -> succeeded | failed | dead`; retryable errors are returned to `queued` before the next attempt.

- **400 (and other terminal 4xx except 429)** → `failed` immediately; retrying invalid input will not fix it.
- **429** → retry, respecting `Retry-After` and exponential backoff.
- **500 / unexpected errors** → retry with bounded exponential backoff.
- **5 attempts maximum**. If retryable errors persist, job becomes **`dead`**; its error and attempts remain in SQLite for inspection.
- The job is claimed via an atomic SQLite transaction. Multiple workers cannot claim the same job **at the same time**. Database claims cap active jobs at 2.
- A 30-second worker lease allows a new worker to reclaim a job if a worker crashes. If the old worker later returns, its stale result is rejected by worker ID check.

A *successful* provider response is charged for its input/output tokens. This mock defines no failure-response token usage, so **failed attempts incur zero recorded token cost**. This is an accounting assumption, not a claim about real-world LLM billing.

## What is implemented

- **Core**: `POST /jobs`, `GET /jobs/{id}`, `GET /healthz`; persisted state; run/test commands; automated tests.
- **Extension A**: separate worker; retry + backoff + Retry-After; Compose; failure-path tests.
- **Extension B (partial)**: idempotency keys; error classification and dead jobs; atomic multi-worker claims; provider-facing concurrency control; signal-based graceful stop between jobs; `/metrics`; per-job and aggregate token/cost accounting.
- **Extension C**: `DESIGN.md` included.

## Deliberately skipped / not production-complete

- **Callbacks:** `callback_url` is stored but no HTTP callback is delivered. Polling is supported and is the primary assignment requirement. A production implementation would validate callback targets to prevent SSRF, use a separate callback queue, retries, and signed payloads.
- **Strict exactly-once provider invocation:** worker crashes after contacting a provider can lead to duplicate provider calls after lease expiry. Processing is at-least-once, not exactly-once.
- **Extended outage and long calls:** fixed 30-second lease has no heartbeat; acceptable for a simulated provider capped at 3s but insufficient for arbitrarily long real calls.
- **Cross-process remote provider concurrency:** global limit is enforced when callers use the SQLite-backed worker path. The in-memory simulated provider alone does not share a semaphore across unrelated processes; a true remote provider would enforce its own global limit.
- **Full production observability, authentication, per-tenant quotas, and external queues** are intentionally out of scope.
- **Execution verified on Windows (Docker Desktop, 24 October 2026):** `docker compose up --build` built and started both API and worker containers; `docker compose ps` showed both Up; `GET /healthz` returned `{"status":"ok"}`; a Docker-hosted summarization job returned `succeeded` with `attempts: 2`, 60 input tokens, 12 output tokens and $0.00036 simulated cost. This documents a manual local run, not an automated CI or cross-platform guarantee.

## Notes for reviewer

The main design choice is a simple SQLite-backed durable job queue. This avoids introducing Redis/Celery solely for this small project and keeps failures observable. `app/worker.py` has no HTTP request dependency. Most tricky behavior is testable with a fake provider; see `tests/test_service.py`.

See `DESIGN.md` for trade-offs, delivery semantics, failure modes, scaling and cost limits.
