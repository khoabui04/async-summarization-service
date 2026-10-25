import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .database import Database


class JobRequest(BaseModel):
    text: str
    callback_url: str | None = None


class JobCreated(BaseModel):
    job_id: str
    status: str


def public_job(job):
    return {k: job[k] for k in ('id', 'status', 'summary', 'error', 'attempts',
            'input_tokens', 'output_tokens', 'cost', 'created_at', 'updated_at')}


def create_app(db_path=None):
    path = db_path or os.getenv('DB_PATH', 'data/jobs.db')
    db = Database(path)
    app = FastAPI(title='Async Summarization Service', version='1.0.0')
    app.state.db = db

    @app.post('/jobs', status_code=status.HTTP_202_ACCEPTED, response_model=JobCreated)
    def create_job(request: JobRequest, idempotency_key: str | None = Header(None)):
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
            raise HTTPException(400, 'invalid_idempotency_key')
        try:
            job, _created = db.create_job(str(uuid.uuid4()), request.text,
                                           request.callback_url, idempotency_key)
        except ValueError:
            raise HTTPException(409, 'idempotency_key_reused_with_different_payload')
        return {'job_id': job['id'], 'status': job['status']}

    @app.get('/jobs/{job_id}')
    def get_job(job_id: str):
        job = db.get(job_id)
        if not job:
            raise HTTPException(404, 'job_not_found')
        return public_job(job)

    @app.get('/healthz')
    def healthz():
        try:
            with db.connect() as conn:
                conn.execute('SELECT 1')
        except Exception:
            raise HTTPException(503, 'database_unavailable')
        return {'status': 'ok'}

    @app.get('/metrics')
    def metrics():
        return db.stats()

    return app


app = create_app()
