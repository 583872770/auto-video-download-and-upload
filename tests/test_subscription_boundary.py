"""Subscription redirects and IPv6 must obey the initial URL boundary."""
import http.server
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp.clash import ClashError, ClashManager, validate_subscription_url


class SubscriptionBoundaryTests(unittest.TestCase):
    def test_ipv6_and_mixed_dns_answers_are_checked_and_dns_failure_is_closed(self):
        for address in ('127.0.0.1', '10.0.0.1', '[::1]', '[::ffff:127.0.0.1]', '[fe80::1]'):
            with self.subTest(address=address), self.assertRaises(ClashError):
                validate_subscription_url('https://' + address + '/subscription')
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
                   (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0))]
        with patch('webapp.clash.socket.getaddrinfo', return_value=answers) as lookup:
            with self.assertRaises(ClashError):
                validate_subscription_url('https://subscription.example/list')
            self.assertNotIn('family', lookup.call_args.kwargs)
        with patch('webapp.clash.socket.getaddrinfo', side_effect=socket.gaierror()):
            with self.assertRaises(ClashError):
                validate_subscription_url('https://subscription.example/list')
        validate_subscription_url('https://[2606:4700:4700::1111]/list')

    def test_redirect_is_rejected_before_target_request_and_preserves_subscription(self):
        visited = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                visited.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/private')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            manager = ClashManager(Path(directory))
            before = (manager.directory / 'settings.json').read_bytes()
            initial = f'http://127.0.0.1:{server.server_port}/start'

            def validate(url):
                # Permit only the fixture's initial request; redirected requests
                # still execute the real validation before any socket is opened.
                if url != initial:
                    validate_subscription_url(url)

            with patch('webapp.clash.validate_subscription_url', side_effect=validate):
                with self.assertRaises(ClashError):
                    manager.import_subscription(initial)
            self.assertEqual(visited, ['/start'])
            self.assertEqual((manager.directory / 'settings.json').read_bytes(), before)
