import math
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app
from app.provider import ProviderError, ProviderResponse, SimulatedProvider
from app.worker import Worker, MAX_ATTEMPTS


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)

    def summarize(self, text, *, request_id, attempt):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / 'jobs.sqlite')
    return TestClient(create_app(path)), Database(path)


def make_job(client, text='Some valid text.'):
    r = client.post('/jobs', json={'text': text})
    assert r.status_code == 202
    return r.json()['job_id']


def test_happy_path_and_accounting(setup):
    client, db = setup
    assert client.get('/healthz').json() == {'status': 'ok'}
    job_id = make_job(client)
    assert client.get(f'/jobs/{job_id}').json()['status'] == 'queued'
    worker = Worker(db, FakeProvider([ProviderResponse('Summary.', 1000, 200)]))
    assert worker.run_once()
    result = client.get(f'/jobs/{job_id}').json()
    assert result['status'] == 'succeeded'
    assert result['summary'] == 'Summary.'
    assert result['attempts'] == 1
    assert result['cost'] == pytest.approx(0.006)
    assert client.get('/metrics').json()['counts']['succeeded'] == 1
    assert not worker.run_once()


def test_bad_input_is_terminal(setup):
    client, db = setup
    job_id = make_job(client, '')
    Worker(db, SimulatedProvider(sleep=lambda _: None)).run_once()
    result = client.get(f'/jobs/{job_id}').json()
    assert result['status'] == 'failed'
    assert result['error'] == 'invalid_input'
    assert result['attempts'] == 1


def test_500_retries_then_succeeds(setup):
    client, db = setup
    job_id = make_job(client)
    provider = FakeProvider([ProviderError(500, 'unavailable'), ProviderResponse('fine', 10, 2)])
    worker = Worker(db, provider)
    worker.run_once()
    assert db.get(job_id)['status'] == 'queued'
    assert db.get(job_id)['attempts'] == 1
    with db.connect() as c:
        c.execute('UPDATE jobs SET next_run=0 WHERE id=?', (job_id,))
    worker.run_once()
    assert db.get(job_id)['status'] == 'succeeded'
    assert db.get(job_id)['attempts'] == 2


def test_429_respects_retry_after(setup):
    client, db = setup
    job_id = make_job(client)
    start = time.time()
    Worker(db, FakeProvider([ProviderError(429, 'rate_limited', retry_after=3)])).run_once()
    job = db.get(job_id)
    assert job['status'] == 'queued'
    assert job['next_run'] >= start + 3


def test_exhausted_job_goes_dead(setup):
    client, db = setup
    job_id = make_job(client)
    worker = Worker(db, FakeProvider([ProviderError(500, 'outage')] * MAX_ATTEMPTS))
    for _ in range(MAX_ATTEMPTS):
        with db.connect() as c:
            c.execute('UPDATE jobs SET next_run=0 WHERE id=?', (job_id,))
        worker.run_once()
    assert db.get(job_id)['status'] == 'dead'
    assert db.get(job_id)['attempts'] == MAX_ATTEMPTS


def test_idempotency_and_conflict(setup):
    client, db = setup
    headers = {'Idempotency-Key': 'same-request'}
    first = client.post('/jobs', json={'text': 'hello'}, headers=headers)
    second = client.post('/jobs', json={'text': 'hello'}, headers=headers)
    assert first.json()['job_id'] == second.json()['job_id']
    assert client.post('/jobs', json={'text': 'different'}, headers=headers).status_code == 409
    assert client.get('/jobs/missing').status_code == 404


def test_survives_restart(setup):
    client, db = setup
    job_id = make_job(client)
    restarted_client = TestClient(create_app(db.path))
    assert restarted_client.get(f'/jobs/{job_id}').json()['status'] == 'queued'


def test_expired_lease_reclaimed(setup):
    client, db = setup
    job_id = make_job(client)
    db.claim('crashed-worker')
    with db.connect() as c:
        c.execute('UPDATE jobs SET lease_until=0 WHERE id=?', (job_id,))
    other = db.claim('new-worker')
    assert other['id'] == job_id
    assert other['attempts'] == 2
    assert db.finish(job_id, 'crashed-worker', status='succeeded') is False


def test_no_duplicate_claim_across_workers(setup):
    client, db = setup
    ids = {make_job(client) for _ in range(10)}
    claimed = []
    lock = threading.Lock()

    def work(i):
        job = Database(db.path).claim(f'worker-{i}')
        with lock:
            if job:
                claimed.append(job['id'])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(claimed) == len(set(claimed)) == 2
    assert set(claimed).issubset(ids)


def test_provider_seed_and_token_formula():
    a = SimulatedProvider(seed='seed-1', sleep=lambda _: None)
    b = SimulatedProvider(seed='seed-1', sleep=lambda _: None)
    assert a.summarize('abcd' * 10, request_id='id', attempt=1) == b.summarize('abcd' * 10, request_id='id', attempt=1)
    result = a.summarize('abcd' * 10, request_id='other', attempt=1)
    assert result.input_tokens == 10
    assert result.output_tokens == 2
