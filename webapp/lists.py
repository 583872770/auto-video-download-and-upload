"""Customer list billing survives pauses; totals are updated with file ledgers."""
from sqlalchemy import select

from webapp.database import batches, counters, download_lists, imports, tasks, deliveries, test_files
from webapp.statistics import charge, pricing, stamp


class DownloadLists:
    def __init__(self, store):
        self.store = store

    def create(self, db, identity, data, name=''):
        customer, label = data.get('customer', ''), data.get('list_name') or name
        if any(not isinstance(value, str) or len(value) > 200 for value in (customer, label)):
            raise ValueError('客户和清单名称最多 200 个字符。')
        self.store.db.ensure(db, batches, dict(id=identity, seq=self.store.db.sequence(db, 'batch'),
                                             created_at=stamp(), name=label))
        defaults = self.store.statistics.defaults(db)
        db.execute(download_lists.insert().values(id=identity, customer=customer.strip(), name=label.strip(),
            released=0, tracking_since=stamp(), **pricing({**defaults, **{key: data[key] for key in ('price', 'unit') if key in data}})))

    def release(self, db):
        db.execute(download_lists.update().where(download_lists.c.released == 0).values(released=1))

    def record_file(self, db, task, size, test):
        if not task.get('batch_id'):
            return
        count, amount = ('test_file_count', 'test_file_bytes') if test else ('file_count', 'file_bytes')
        db.execute(download_lists.update().where(download_lists.c.id == task['batch_id']).values(
            {count: download_lists.c[count] + 1, amount: download_lists.c[amount] + size}))

    def outcome(self, db, row, outcome):
        if row.get('batch_id') and outcome in ('skipped', 'test_succeeded'):
            db.execute(download_lists.update().where(download_lists.c.id == row['batch_id']).values(
                {outcome: download_lists.c[outcome] + 1}))

    def page(self, cursor=None, query='', limit=20):
        statement = select(batches, *[column for column in download_lists.c if column.name not in ('id', 'name')],
            download_lists.c.name.label('list_name'), imports.c.processed, imports.c.added,
            imports.c.duplicates, imports.c.invalid, imports.c.finished.label('import_finished')).select_from(
                batches.outerjoin(download_lists, batches.c.id == download_lists.c.id)
                       .outerjoin(imports, batches.c.id == imports.c.id)).order_by(batches.c.seq.desc()).limit(limit + 1)
        if cursor:
            statement = statement.where(batches.c.seq < self.store.statistics.cursor(cursor))
        if query:
            from sqlalchemy import or_
            statement = statement.where(or_(download_lists.c.customer.contains(query[:200], autoescape=True),
                batches.c.name.contains(query[:200], autoescape=True),
                download_lists.c.name.contains(query[:200], autoescape=True)))
        with self.store.connect() as db:
            rows = db.execute(statement).mappings().all()
            ids = [row['id'] for row in rows[:limit]]
            counts = {}
            if ids:
                for row in db.execute(select(counters).where(counters.c.scope.in_(['batch:' + identity for identity in ids]))).mappings():
                    counts.setdefault(row['scope'][6:], {})[row['status']] = int(row['value'])
        items = []
        for row in rows[:limit]:
            value = dict(row)
            value['tracked'] = row['tracking_since'] is not None
            for key in ('file_count', 'file_bytes', 'test_file_count', 'test_file_bytes', 'test_succeeded', 'skipped'):
                value[key] = int(row[key] or 0)
            state = counts.get(row['id'], {})
            value.update(queued=state.get('queued', 0), running=state.get('running', 0),
                failed=state.get('failed', 0) + state.get('interrupted', 0), total=sum(state.values()))
            value['succeeded'] = max(0, state.get('succeeded', 0) - value['test_succeeded'] - value['skipped'])
            value['name'] = row['list_name'] or row['name'] or '未命名清单'
            value['file_bytes_exact'], value['test_file_bytes_exact'] = str(value['file_bytes']), str(value['test_file_bytes'])
            value['amount'] = str(charge(value['file_bytes'], row['price'] or '0', row['unit'] or 'GB'))
            items.append(value)
        return dict(items=items, next_cursor=str(rows[limit - 1]['seq']) if len(rows) > limit else None)

    def update(self, identity, data):
        customer, name = data.get('customer', ''), data.get('name', '')
        if any(not isinstance(value, str) or len(value) > 200 for value in (customer, name)):
            raise ValueError('客户和清单名称最多 200 个字符。')
        with self.store.connect() as db:
            if not db.execute(select(download_lists.c.id).where(download_lists.c.id == identity)).first():
                raise ValueError('历史清单尚无独立账本，无法修改其计费设置。')
            db.execute(download_lists.update().where(download_lists.c.id == identity).values(
                customer=customer.strip(), name=name.strip(), **pricing(data)))

    def files(self, identity, test=False, cursor=None, limit=1000):
        ledger = test_files if test else deliveries
        statement = select(ledger).select_from(ledger.join(tasks, tasks.c.id == ledger.c.task_id)).where(
            tasks.c.batch_id == identity).order_by(ledger.c.seq.desc()).limit(limit)
        if cursor:
            statement = statement.where(ledger.c.seq < cursor)
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(statement).mappings()]
