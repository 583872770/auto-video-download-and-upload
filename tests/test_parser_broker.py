"""IPC ownership, cancellation and reuse without external conversion jobs."""
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import psutil

from webapp.bergung import BergungError
from webapp.parser_broker import ParserBroker, cancellable, resolve_remote


class BrokerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.broker = ParserBroker(self.directory, lambda: {'yt5s_browser_count': 1, 'worker_count': 4})
        self.broker.start()
        self.addCleanup(self.broker.close)

    def test_reuses_slot_and_session_for_different_download_owners(self):
        owners = []
        def fake(*args, **kwargs):
            owners.append(args[1]['_parser_session'])
            args[4]('waiting_verification')
            return {'url': 'https://media.example.com/file?token=PRIVATE', 'id': '00000000001'}
        stages = []
        with patch('webapp.bergung.resolve', side_effect=fake):
            for _ in range(2):
                result = resolve_remote('https://youtu.be/00000000001', {}, self.directory, self.directory, stages.append)
                self.assertIn('PRIVATE', result['url'])
        self.assertIs(owners[0], owners[1])
        self.assertIn('waiting_verification', stages)
        self.assertNotIn('PRIVATE', (self.directory/'parser-broker.json').read_text())

    def test_typed_rate_limit_reaches_task_scheduler(self):
        with patch('webapp.bergung.resolve', side_effect=BergungError('rate limit', retryable=True, http_status=429, retry_after=75)):
            with self.assertRaises(BergungError) as caught:
                resolve_remote('https://youtu.be/00000000001', {}, self.directory, self.directory)
        self.assertEqual((caught.exception.http_status, caught.exception.retry_after), (429, 75))

    def test_client_disconnect_cancels_only_its_own_work_and_releases_slot(self):
        entered, cancelled = threading.Event(), threading.Event()
        async def work():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        def fake(url, config, *args, **kwargs):
            return asyncio.run(cancellable(work(), config['_parser_cancel_check']))
        address = json.loads((self.directory/'parser-broker.json').read_text())
        connection = socket.create_connection(('127.0.0.1', address['port']))
        message = dict(token=address['token'], pid=os.getpid(), created=psutil.Process().create_time(),
                       config={}, url='https://youtu.be/00000000001', base=str(self.directory))
        with patch('webapp.bergung.resolve', side_effect=fake):
            connection.sendall((json.dumps(message)+'\n').encode())
            self.assertTrue(entered.wait(3))
            connection.close()
            self.assertTrue(cancelled.wait(3))
        with patch('webapp.bergung.resolve', return_value={'id': 'next'}):
            self.assertEqual(resolve_remote('https://youtu.be/00000000002', {}, self.directory, self.directory)['id'], 'next')

    def test_invalid_auth_never_invokes_resolver(self):
        address = json.loads((self.directory/'parser-broker.json').read_text())
        with patch('webapp.bergung.resolve') as resolve, socket.create_connection(('127.0.0.1', address['port'])) as connection:
            connection.sendall(b'{"token":"invalid"}\n')
            connection.settimeout(2)
            self.assertEqual(connection.recv(1024), b'')
        resolve.assert_not_called()

    def test_resource_errors_and_route_classification_survive_ipc(self):
        from webapp.disk_budget import DiskPressure
        from webapp.proxy_health import connection_failure
        with patch('webapp.bergung.resolve',side_effect=DiskPressure('disk budget full')):
            with self.assertRaises(DiskPressure):
                resolve_remote('https://youtu.be/00000000001',{},self.directory,self.directory)
        with patch('webapp.bergung.resolve',side_effect=RuntimeError('Page.goto net::ERR_CONNECTION_CLOSED')):
            with self.assertRaises(BergungError) as caught:
                resolve_remote('https://youtu.be/00000000001',{},self.directory,self.directory)
        self.assertTrue(connection_failure(caught.exception))
