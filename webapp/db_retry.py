"""Retry only rolled-back database work, never external actions or lost connections."""
import random
import time

from sqlalchemy.exc import OperationalError


def transaction_retry(operation, attempts=3):
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as error:
            code = error.orig.args[0] if getattr(error.orig, 'args', ()) else None
            retryable = code in (1205, 1213) or 'database is locked' in str(error.orig).lower()
            if not retryable or attempt + 1 == attempts:
                raise
            time.sleep(random.uniform(.025, .075) * 2 ** attempt)
