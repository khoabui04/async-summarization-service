import logging
import os
import signal
import time
import uuid

from .database import Database, now
from .provider import ProviderError, SimulatedProvider, SummarizationProvider

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class Worker:
    def __init__(self, db: Database, provider: SummarizationProvider | None = None, worker_id=None):
        self.db = db
        self.provider = provider if provider is not None else SimulatedProvider()
        self.worker_id = worker_id or str(uuid.uuid4())
        self.stopping = False

    def run_once(self) -> bool:
        job = self.db.claim(self.worker_id)
        if not job:
            return False
        try:
            result = self.provider.summarize(job['text'], request_id=job['id'], attempt=job['attempts'])
            cost = result.input_tokens * 0.003 / 1000 + result.output_tokens * 0.015 / 1000
            self.db.finish(job['id'], self.worker_id, status='succeeded', summary=result.summary,
                           error=None, input_tokens=result.input_tokens,
                           output_tokens=result.output_tokens, cost=cost)
        except ProviderError as e:
            terminal = 400 <= e.status_code < 500 and e.status_code != 429
            if terminal:
                self.db.finish(job['id'], self.worker_id, status='failed', error=e.code)
            elif job['attempts'] >= MAX_ATTEMPTS:
                self.db.finish(job['id'], self.worker_id, status='dead', error=e.code)
            else:
                delay = max(float(e.retry_after or 0), min(2 ** (job['attempts'] - 1), 30))
                self.db.finish(job['id'], self.worker_id, status='queued',
                               next_run=now() + delay, error=e.code)
        except Exception:
            logger.exception('Unexpected provider error for job %s', job['id'])
            if job['attempts'] >= MAX_ATTEMPTS:
                self.db.finish(job['id'], self.worker_id, status='dead', error='internal_error')
            else:
                self.db.finish(job['id'], self.worker_id, status='queued',
                               next_run=now() + min(2 ** (job['attempts'] - 1), 30), error='internal_error')
        return True

    def run_forever(self):
        while not self.stopping:
            if not self.run_once():
                time.sleep(0.2)


def main():
    logging.basicConfig(level=logging.INFO)
    db = Database(os.getenv('DB_PATH', 'data/jobs.db'))
    worker = Worker(db)
    def stop(_signum, _frame):
        worker.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info('Worker started: %s', worker.worker_id)
    worker.run_forever()


if __name__ == '__main__':
    main()
