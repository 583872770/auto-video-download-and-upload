"""The customer task ledger is the only source of current estimated charges."""
from decimal import Decimal

from sqlalchemy import select

from webapp.database import download_lists, job_profiles
from webapp.statistics import charge


class BillingService:
    def __init__(self, store):
        self.store = store

    def summary(self, db):
        # Round once per job, exactly as in job detail and its bill export.
        # Archived/deleted jobs retain their historical delivery and estimate.
        rows = db.execute(select(download_lists.c.file_bytes, download_lists.c.price,
            download_lists.c.unit, job_profiles.c.price_set).select_from(
            download_lists.outerjoin(job_profiles, job_profiles.c.id == download_lists.c.id))).mappings()
        size, amount, unset, unpriced = 0, Decimal('0.00'), 0, 0
        for row in rows:
            size += row['file_bytes']
            if row['price_set'] == 0:
                unset += 1
                unpriced += row['file_bytes']
            else:
                amount += charge(row['file_bytes'], row['price'], row['unit'])
        return dict(file_bytes_exact=str(size), amount=format(amount, '.2f'), unset=unset,
            unpriced_bytes=unpriced, unpriced_bytes_exact=str(unpriced),
            scope='all_customer_jobs', estimated=True)
