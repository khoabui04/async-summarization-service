import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).timestamp()


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 10000')
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    callback_url TEXT,
                    idem_key TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    summary TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    next_run REAL NOT NULL,
                    lease_until REAL,
                    worker_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_work ON jobs(status, next_run);
            ''')

    def create_job(self, job_id, text, callback_url=None, idem_key=None):
        t = now()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if idem_key:
                existing = c.execute('SELECT * FROM jobs WHERE idem_key=?', (idem_key,)).fetchone()
                if existing:
                    if existing['text'] != text or existing['callback_url'] != callback_url:
                        c.rollback()
                        raise ValueError('idempotency_key_reused_with_different_payload')
                    c.commit()
                    return dict(existing), False
            c.execute('''INSERT INTO jobs(id, text, callback_url, idem_key, next_run, created_at, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''', (job_id, text, callback_url, idem_key, t, t, t))
            result = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            c.commit()
            return result, True

    def get(self, job_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return dict(row) if row else None

    def claim(self, worker_id, *, max_running=2, lease_seconds=30):
        t = now()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # Expired leases are reclaimed. Processing is at-least-once on worker crashes.
            c.execute('''UPDATE jobs SET status='queued', worker_id=NULL, lease_until=NULL,
                         next_run=?, updated_at=?, error='worker_lease_expired'
                         WHERE status='running' AND lease_until < ?''', (t, t, t))
            count = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            if count >= max_running:
                c.commit()
                return None
            row = c.execute('''SELECT id FROM jobs WHERE status='queued' AND next_run<=?
                             ORDER BY next_run, created_at LIMIT 1''', (t,)).fetchone()
            if not row:
                c.commit()
                return None
            c.execute('''UPDATE jobs SET status='running', attempts=attempts+1,
                         worker_id=?, lease_until=?, updated_at=? WHERE id=?''',
                      (worker_id, t + lease_seconds, t, row['id']))
            claimed = dict(c.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
            c.commit()
            return claimed

    def finish(self, job_id, worker_id, **values):
        valid = {'status', 'summary', 'error', 'input_tokens', 'output_tokens', 'cost', 'next_run'}
        if not values.keys() <= valid:
            raise ValueError('unexpected job fields')
        fields = ', '.join(f'{name}=?' for name in values)
        params = list(values.values())
        with self.connect() as c:
            cur = c.execute(f'''UPDATE jobs SET {fields}, worker_id=NULL, lease_until=NULL,
                                  updated_at=? WHERE id=? AND worker_id=? AND status='running' ''',
                            (*params, now(), job_id, worker_id))
            return cur.rowcount == 1

    def stats(self):
        with self.connect() as c:
            counts = {r['status']: r['n'] for r in c.execute('SELECT status, COUNT(*) n FROM jobs GROUP BY status')}
            totals = c.execute('SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens FROM jobs').fetchone()
            return {'counts': counts, 'totals': dict(totals)}
