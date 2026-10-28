"""The customer task ledger is the only source of current estimated charges."""
from decimal import Decimal

from sqlalchemy import case, func, or_, select

from webapp.database import download_lists, job_profiles
from webapp.statistics import charge


class BillingService:
    def __init__(self, store):
        self.store = store

    def summary(self, db):
        # Archived/deleted jobs retain their historical delivery and estimate.
        source = download_lists.outerjoin(job_profiles, job_profiles.c.id == download_lists.c.id)
        unset_job = job_profiles.c.price_set == 0
        totals = db.execute(select(
            func.coalesce(func.sum(download_lists.c.file_bytes), 0).label('size'),
            func.coalesce(func.sum(case((unset_job, 1), else_=0)), 0).label('unset'),
            func.coalesce(func.sum(case((unset_job, download_lists.c.file_bytes), else_=0)), 0).label('unpriced'),
        ).select_from(source)).mappings().one()
        # Round once per job with Decimal ROUND_HALF_UP, exactly as in job
        # detail and its bill export. SQL ROUND is not used: SQLite divides in
        # floating point and misrounds half-cent amounts. Only priced jobs with
        # deliveries are transferred; jobs without a profile keep their price.
        priced = select(download_lists.c.file_bytes, download_lists.c.price, download_lists.c.unit).select_from(
            source).where(download_lists.c.file_bytes > 0, or_(job_profiles.c.price_set != 0, job_profiles.c.id.is_(None)))
        amount = sum((charge(row.file_bytes, row.price, row.unit) for row in db.execute(priced)), Decimal('0.00'))
        size, unpriced = int(totals['size']), int(totals['unpriced'])
        return dict(file_bytes_exact=str(size), amount=format(amount, '.2f'), unset=int(totals['unset']),
            unpriced_bytes=unpriced, unpriced_bytes_exact=str(unpriced),
            scope='all_customer_jobs', estimated=True)
