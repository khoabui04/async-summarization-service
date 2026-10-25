"""Swappable simulated remote provider; no real LLM or API keys required."""
import hashlib
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProviderResponse:
    summary: str
    input_tokens: int
    output_tokens: int


class ProviderError(Exception):
    def __init__(self, status_code: int, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


class SummarizationProvider(Protocol):
    def summarize(self, text: str, *, request_id: str, attempt: int) -> ProviderResponse: ...


class SimulatedProvider:
    # Limits calls within this provider instance. Global cross-worker safety is
    # provided by the database claim limit of two active jobs.
    def __init__(self, seed: str | None = None, sleep=time.sleep):
        self.seed = seed if seed is not None else os.getenv('SEED', '42')
        self.sleep = sleep
        self.slots = threading.BoundedSemaphore(2)

    def summarize(self, text: str, *, request_id: str, attempt: int) -> ProviderResponse:
        if not text.strip() or len(text) > 10_000:
            raise ProviderError(400, 'invalid_input')
        digest = hashlib.sha256(f'{self.seed}:{request_id}:{attempt}'.encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], 'big'))
        if not self.slots.acquire(blocking=False):
            raise ProviderError(429, 'rate_limited', retry_after=rng.randint(1, 3))
        try:
            self.sleep(rng.uniform(1, 3))
            if rng.random() < 0.15:
                raise ProviderError(500, 'provider_unavailable')
            inputs = math.ceil(len(text) / 4)
            outputs = math.ceil(inputs * 0.2)
            # Summary quality is explicitly outside the scope of this assessment.
            sentences = [s.strip() for s in text.split('.') if s.strip()]
            summary = '. '.join(sentences[:2])[:500]
            if summary and not summary.endswith('.'):
                summary += '.'
            return ProviderResponse(summary, inputs, outputs)
        finally:
            self.slots.release()
