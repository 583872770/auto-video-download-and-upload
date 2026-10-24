"""Billing follows durable final files, with transactional pause/retry boundaries."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import http.server
import csv
from io import StringIO
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_scale
from tests.test_mysql import MySQLFixture
from webapp.database import archive, deliveries, download_sessions, settings, tasks, test_files, test_totals
from webapp.statistics import describe
from webapp.store import TaskStore
from web_worker import execute


class StatisticsContract:
    def job(self, video='npNnbj46_ik', **config):
        return self.store.create('download', {'config': {'path': 'videos', 'proxy_mode': 'direct', **config},
            'base': str(self.root), 'urls': ['https://www.youtube.com/watch?v=' + video]})

    def overview(self):
        response = self.client.get('/api/statistics')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def claim(self):
        return self.store.claim(self.store.owner)

    def final_file(self, name='first.mp4', size=1024):
        path = self.root / name
        path.write_bytes(b'x' * size)
        return path

    def test_pause_resume_preserves_session_history_and_counts_finished_video_once(self):
        for video in ('npNnbj46_ik', '2Gu7q-KUXCk', 'WMu6ANTg8mk'):
            self.job(video)
        first, second = self.claim(), self.claim()
        identity = first['payload']['download_session_id']
        self.assertEqual(second['payload']['download_session_id'], identity)
        path = self.final_file(size=12345)
        self.assertTrue(self.store.statistics.record_file(first['id'], path, 'npNnbj46_ik', 'Client video'))
        self.store.finish(first['id'], result={}, worker_id=self.store.owner)
        self.store.finish(second['id'], error='permanent failure', worker_id=self.store.owner)
        third = self.claim()
        self.store.set_downloads_paused(True)
        self.assertEqual(self.store.statistics.session(identity)['status'], 'stopping')
        self.store.stop_running_downloads(lambda owner: True)
        closed = self.store.statistics.session(identity)
        self.assertEqual((closed['attempted'], closed['succeeded'], closed['failed'], closed['unfinished']), (3, 1, 1, 1))
        self.assertEqual((closed['status'], closed['file_count'], closed['file_bytes']), ('paused', 1, 12345))
        self.assertIsNotNone(closed['ended_at'])
        self.assertFalse(self.store.finish(third['id'], result={}, worker_id='old-executor'))
        self.store.set_downloads_paused(False)
        resumed = self.claim()
        new_id = resumed['payload']['download_session_id']
        self.assertNotEqual(new_id, identity)
        # A duplicate callback, even in another session, cannot bill the same path twice.
        self.assertFalse(self.store.statistics.record_file(resumed['id'], path))
        next_file = self.final_file('next.mp4', 5678)
        self.assertTrue(self.store.statistics.record_file(resumed['id'], next_file))
        self.store.finish(resumed['id'], result={}, worker_id=self.store.owner)
        self.assertEqual(self.store.statistics.session(new_id)['status'], 'completed')
        self.assertEqual(self.store.statistics.session(identity), closed)
        overview = self.overview()
        self.assertEqual((overview['summary']['file_count'], overview['summary']['file_bytes']), (2, 18023))
        self.assertEqual(len(overview['items']), 2)
        self.assertIsNone(overview['active'])
        self.assertEqual(TaskStore(self.store.directory).statistics.overview()['summary'], overview['summary'])

    def test_retries_do_not_multiply_attempted_count_or_failure_count(self):
        self.job()
        first = self.claim()
        identity = first['payload']['download_session_id']
        self.store.finish(first['id'], error='temporary gateway failure', retryable=True, worker_id=self.store.owner)
        current = self.store.statistics.session(identity)
        self.assertEqual((current['attempted'], current['failed'], current['running'], current['unfinished']), (1, 0, 0, 1))
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == first['id']).values(available_at=0))
        again = self.claim()
        self.assertEqual(again['payload']['download_session_id'], identity)
        self.assertEqual(self.store.statistics.session(identity)['attempted'], 1)
        self.store.finish(again['id'], error='permanent failure', worker_id=self.store.owner)
        closed = self.store.statistics.session(identity)
        self.assertEqual((closed['attempted'], closed['failed'], closed['file_bytes'], closed['status']), (1, 1, 0, 'completed'))
        self.assertFalse(self.store.finish(again['id'], error='duplicate completion', worker_id=self.store.owner))
        self.assertEqual(self.store.statistics.session(identity), closed)

    def test_parallel_callbacks_are_idempotent_and_invalid_owners_are_fenced(self):
        self.job()
        job = self.claim()
        path = self.final_file()
        other = TaskStore(self.store.directory)
        with self.assertRaisesRegex(RuntimeError, '租约'):
            other.statistics.record_file(job['id'], path)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.store.statistics.record_file(job['id'], path), range(2)))
        self.assertEqual(sorted(results), [False, True])
        current = self.store.statistics.session(job['payload']['download_session_id'])
        self.assertEqual((current['file_count'], current['file_bytes']), (1, 1024))

    def test_pricing_csrf_rounding_units_and_invalid_input_leave_totals_intact(self):
        route = '/api/statistics/pricing'
        self.assertEqual(self.client.put(route, json={'price': '2', 'unit': 'GB'}).status_code, 403)
        response = self.client.put(route, json={'price': '12.345678', 'unit': 'GiB'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.job()
        job = self.claim()
        identity = job['payload']['download_session_id']
        current = self.store.statistics.session(identity)
        self.assertEqual((current['price'], current['unit']), ('12.345678', 'GiB'))
        self.store.statistics.record_file(job['id'], self.final_file(size=1000))
        route = '/api/statistics/sessions/' + identity
        response = self.client.put(route, json={'price': '15000', 'unit': 'GB', 'label': '客户 A · 清单一'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['session']['amount'], '0.02')  # Round 0.015 yuan once per session.
        before = self.store.statistics.session(identity)
        for values in ({'price': '-1'}, {'price': 'NaN'}, {'price': 'Infinity'}, {'price': '0.0000001'},
                       {'price': '1000001'}, {'price': '1', 'unit': 'TB'}, {'price': '1', 'label': 'x' * 201}):
            self.assertEqual(self.client.put(route, json=values, headers=self.headers).status_code, 400, values)
            self.assertEqual(self.store.statistics.session(identity), before)
        self.assertEqual(self.client.get('/api/statistics/sessions/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/statistics?cursor=invalid').status_code, 400)
        self.assertEqual(self.client.get(route + '?kind=invalid').status_code, 400)

    def test_paginated_exports_keep_exact_bytes_and_neutralize_spreadsheet_formulas(self):
        self.job()
        job = self.claim()
        identity = job['payload']['download_session_id']
        with self.store.connect() as db:
            db.execute(deliveries.insert(), [{'id': f'{index:064d}', 'seq': index + 1, 'session_id': identity,
                'task_id': job['id'], 'video_id': 'npNnbj46_ik', 'title': '=unsafe-formula',
                'path': f'videos/{index}.mp4', 'size': 1000 + index, 'created_at': '2026-10-24T12:00:00+08:00'}
                for index in range(1003)])
        route = '/api/statistics/sessions/' + identity
        first = self.client.get(route).json
        self.assertEqual(len(first['items']), 50)
        second = self.client.get(route + '?cursor=' + first['next_cursor']).json
        self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
        exported = self.client.get(route + '/export')
        self.assertEqual(exported.status_code, 200)
        text = exported.data.decode('utf-8-sig')
        self.assertEqual(len(text.splitlines()), 1004)
        self.assertIn("'=unsafe-formula", text)
        self.assertIn(',2002,0.000002002,', text)
        failed = self.client.get(route + '/export?kind=failed')
        self.assertEqual(len(failed.data.decode('utf-8-sig').splitlines()), 1)

    def test_dashboard_reads_only_statistics_tables_not_entire_task_history(self):
        self.job()
        self.claim()
        statements = []

        def sql(db, cursor, statement, parameters, context, many):
            statements.append(statement.lower())

        event.listen(self.store.db.engine, 'before_cursor_execute', sql)
        try:
            self.overview()
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', sql)
        self.assertFalse(any(any(table in statement for table in ('from tasks', 'from download_deliveries', 'from download_test_files'))
                             for statement in statements))

    def test_customer_filter_and_summary_export_combine_sessions_with_different_rates(self):
        for video, label, price in [('npNnbj46_ik', '客户 A 清单一', '15000'),
                                    ('2Gu7q-KUXCk', '客户 B', '90000'), ('WMu6ANTg8mk', '客户 A 清单二', '5000')]:
            self.job(video)
            job = self.claim()
            self.store.statistics.record_file(job['id'], self.final_file(video + '.mp4', 1000))
            self.store.statistics.update(job['payload']['download_session_id'], {'label': label, 'price': price, 'unit': 'GB'})
            self.store.finish(job['id'], result={}, worker_id=self.store.owner)
        filtered = self.client.get('/api/statistics', query_string={'q': '客户 A'}).json
        self.assertEqual(len(filtered['items']), 2)
        self.assertEqual((filtered['summary']['file_count'], filtered['summary']['file_bytes']), (2, 2000))
        self.assertEqual(filtered['summary']['amount'], '0.03')  # 0.015 -> .02 and 0.005 -> .01.
        summary = self.client.get('/api/statistics/export', query_string={'q': '客户 A'}).data.decode('utf-8-sig')
        self.assertEqual(len(summary.splitlines()), 3)
        self.assertNotIn('客户 B', summary)
        self.assertIn(',1,1,0,0,0,0,1,1000,', summary)  # Numeric zeroes survive CSV export.
        literal = self.client.get('/api/statistics', query_string={'q': '%'}).json
        self.assertEqual(literal['items'], [])

    def test_large_billing_totals_preserve_bytes_beyond_browser_number_precision(self):
        self.job()
        identity = self.claim()['payload']['download_session_id']
        size = 9007199254740993
        with self.store.connect() as db:
            db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(file_bytes=size, price='2.5'))
        data = self.overview()
        self.assertEqual(data['summary']['file_bytes_exact'], str(size))
        self.assertEqual(data['items'][0]['file_bytes_exact'], str(size))
        self.assertEqual(data['items'][0]['amount'], '22517998.14')

    def test_deleted_test_files_accumulate_per_session_and_never_increase_billing(self):
        for video, label, size in [('npNnbj46_ik', '客户 A 测试一', 12345), ('2Gu7q-KUXCk', '客户 B 测试', 4567),
                                   ('WMu6ANTg8mk', '客户 A 测试二', 6789)]:
            self.job(video, storage_mode='test')
            job = self.claim()
            identity = job['payload']['download_session_id']
            self.store.statistics.update(identity, {'label': label, 'price': '999999', 'unit': 'GB'})
            path = self.final_file(video + '.mp4', size)
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: self.store.statistics.record_file(job['id'], path, video,
                                             'Test video', test_mode=True), range(2)))
            self.assertEqual(sorted(results), [False, True])
            # The ledger survives deletion and a subsequent worker failure/retry.
            path.unlink()
            self.store.finish(job['id'], error='temporary network failure', retryable=True, worker_id=self.store.owner)
            self.assertEqual(self.store.statistics.session(identity)['test_file_bytes'], size)
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == job['id']).values(available_at=0))
            resumed = self.claim()
            path = self.final_file(video + '.mp4', size)
            self.assertFalse(self.store.statistics.record_file(resumed['id'], path, test_mode=True))
            path.unlink()
            self.store.finish(job['id'], result={'test_mode': True}, worker_id=self.store.owner)
            row = self.store.statistics.session(identity)
            self.assertEqual((row['test_succeeded'], row['test_file_count'], row['test_file_bytes']), (1, 1, size))
            self.assertEqual((row['file_count'], row['file_bytes'], row['amount']), (0, 0, '0.00'))
            self.assertEqual(row['success_rate'], 100)
            detail = self.client.get('/api/statistics/sessions/' + identity + '?kind=test_files').json
            self.assertEqual((len(detail['items']), detail['items'][0]['size_exact']), (1, str(size)))
            self.assertEqual(self.client.get('/api/statistics/sessions/' + identity).json['items'], [])
            exported = self.client.get('/api/statistics/sessions/' + identity + '/export?kind=test_files').data.decode('utf-8-sig')
            rows = list(csv.DictReader(StringIO(exported)))
            self.assertEqual((rows[0]['精确字节数'], rows[0]['单价(元/单位)']), (str(size), '0'))
            self.assertIn('不计费', rows[0]['保存方式'])
        filtered = self.client.get('/api/statistics', query_string={'q': '客户 A'}).json
        summary = filtered['summary']
        self.assertEqual((summary['test_file_count'], summary['test_file_bytes'], summary['downloaded_file_bytes']), (2, 19134, 19134))
        self.assertEqual(summary['amount'], '0.00')
        self.assertEqual(self.overview()['summary']['test_file_bytes'], 23701)
        self.assertEqual(TaskStore(self.store.directory).statistics.overview()['summary']['test_file_count'], 3)
        exported = self.client.get('/api/statistics/export', query_string={'q': '客户 A'}).data.decode('utf-8-sig')
        rows = list(csv.DictReader(StringIO(exported)))
        self.assertEqual(sum(int(row['测试精确字节数(不计费)']) for row in rows), 19134)

    def test_repeated_test_video_in_distinct_tasks_counts_each_run_and_pause_keeps_sizes(self):
        path = self.final_file(size=8000)
        for index in range(2):
            self.job(storage_mode='test')
            job = self.claim()
            identity = job['payload']['download_session_id']
            self.store.statistics.record_file(job['id'], path, test_mode=True)
            self.store.set_downloads_paused(True)
            self.store.finish(job['id'], result={'test_mode': True}, worker_id=self.store.owner)
            row = self.store.statistics.session(identity)
            self.assertEqual((row['status'], row['test_file_count'], row['test_file_bytes']), ('paused', 1, 8000))
            self.store.set_downloads_paused(False)
        data = self.overview()
        self.assertEqual((data['summary']['test_file_count'], data['summary']['test_file_bytes']), (2, 16000))
        self.assertEqual(len(data['items']), 2)

    def test_test_sizes_preserve_large_totals_and_old_databases_upgrade_without_changing_billing(self):
        self.job()
        job = self.claim()
        identity = job['payload']['download_session_id']
        self.store.statistics.record_file(job['id'], self.final_file(size=1000))
        self.store.statistics.update(identity, {'label': '历史会话', 'price': '15000', 'unit': 'GB'})
        # Simulate the previous deployed schema, which has billing but no test tables.
        test_files.drop(self.store.db.engine)
        test_totals.drop(self.store.db.engine)
        with self.store.connect() as db:
            db.execute(settings.delete().where(settings.c.key == 'download_test_statistics_since'))
        self.store.db.initialize()
        self.store.db.initialize()
        current = self.store.statistics.session(identity)
        self.assertEqual((current['file_bytes'], current['test_file_bytes'], current['amount']), (1000, 0, '0.02'))
        size = 9007199254740993
        with self.store.connect() as db:
            db.execute(test_totals.insert().values(session_id=identity, test_file_count=1, test_file_bytes=size))
        data = self.overview()
        self.assertEqual(data['summary']['test_file_bytes_exact'], str(size))
        self.assertEqual(data['items'][0]['test_file_bytes_exact'], str(size))
        self.assertEqual(data['summary']['downloaded_file_bytes_exact'], str(size + 1000))
        self.assertEqual(data['summary']['amount'], '0.02')
        self.assertTrue(data['test_tracking_since'])


class SQLiteStatisticsTests(StatisticsContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_actual_final_files_count_but_test_download_and_archive_skip_do_not_bill(self):
        content = b'actual final video fixture\n' * 201

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        identity = None
        for index, (name, mode) in enumerate([('normal', 'local'), ('test', 'test'), ('normal', 'local'), ('normal', 'local')]):
            if index == 3:
                with self.store.connect() as db:
                    db.execute(archive.delete())  # A pre-existing file must not be billed without its archive entry.
            self.store.set_runtime({'storage_mode': mode})
            self.store.create('download', {'config': {'path': 'videos', 'proxy_mode': 'direct', 'storage_mode': mode},
                'base': str(self.root), 'urls': [f'http://127.0.0.1:{server.server_port}/{name}.mp4']})
            job = self.claim()
            with patch('Download.require_download_tools'):
                result = execute('download', job['payload'], store=self.store, task_id=job['id'])
            self.store.finish(job['id'], result=result, worker_id=self.store.owner)
            if identity is None:
                identity = job['payload']['download_session_id']
                self.assertEqual(result['billable_bytes'], len(content))
            elif mode == 'test':
                self.assertTrue(result['files_deleted'])
                self.assertEqual(result['billable_bytes'], 0)
            else:
                self.assertTrue(result['skipped'])
        totals = self.overview()['summary']
        self.assertEqual((totals['file_count'], totals['file_bytes']), (1, len(content)))
        self.assertEqual((totals['succeeded'], totals['test_succeeded'], totals['skipped']), (1, 1, 2))
        self.assertEqual((totals['test_file_count'], totals['test_file_bytes']), (1, len(content)))
        self.assertEqual((totals['downloaded_file_count'], totals['downloaded_file_bytes']), (2, len(content) * 2))
        self.assertFalse(list((self.store.directory / 'test-downloads').rglob('*.mp4')))


class MySQLStatisticsTests(MySQLFixture, StatisticsContract, unittest.TestCase):
    pass
