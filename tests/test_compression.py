import gzip
from io import BytesIO
import unittest

from flask import Flask, Response, jsonify, send_file

from webapp.compression import register_compression


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        register_compression(self.app)

        @self.app.get('/api/example')
        def example():
            return jsonify(items=[dict(title='视频', value=index) for index in range(100)])

        @self.app.get('/static/example.js')
        def static_asset():
            response = send_file(BytesIO(b'const value = 123;\n' * 200), mimetype='text/javascript',
                                 download_name='example.js', etag='asset-v1')
            response.cache_control.no_cache = True
            return response

        @self.app.get('/stream')
        def stream():
            return Response((b'a' * 2048 for _ in range(2)), mimetype='application/json')

        @self.app.get('/download')
        def download():
            return send_file(BytesIO(b'a' * 4096), mimetype='text/plain', as_attachment=True, download_name='test.txt')

        @self.app.get('/encoded')
        def encoded():
            return Response(gzip.compress(b'a' * 4096), mimetype='application/json', headers={'Content-Encoding': 'gzip'})

        self.client = self.app.test_client()

    def test_json_round_trip_negotiation_and_content_length(self):
        original = self.client.get('/api/example')
        compressed = self.client.get('/api/example', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(gzip.decompress(compressed.data), original.data)
        self.assertEqual(compressed.content_length, len(compressed.data))
        self.assertLess(len(compressed.data), len(original.data) // 3)
        self.assertIn('Accept-Encoding', compressed.vary)
        for encoding in ('identity', 'gzip;q=0, identity;q=1'):
            response = self.client.get('/api/example', headers={'Accept-Encoding': encoding})
            self.assertNotIn('Content-Encoding', response.headers)
            self.assertEqual(response.data, original.data)

    def test_static_range_and_cache_validation(self):
        compressed = self.client.get('/static/example.js', headers={'Accept-Encoding': 'gzip'})
        self.assertTrue(compressed.headers['Content-Disposition'].startswith('inline;'))
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertTrue(compressed.get_etag()[1])
        self.assertEqual(gzip.decompress(compressed.data), b'const value = 123;\n' * 200)
        partial = self.client.get('/static/example.js', headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-9'})
        self.assertEqual(partial.status_code, 206)
        self.assertNotIn('Content-Encoding', partial.headers)
        self.assertEqual(len(partial.data), 10)
        unchanged = self.client.get('/static/example.js', headers={
            'Accept-Encoding': 'gzip', 'If-None-Match': compressed.headers['ETag']})
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.data, b'')

    def test_stream_download_and_existing_encoding_are_preserved(self):
        for path in ('/stream', '/download'):
            response = self.client.get(path, headers={'Accept-Encoding': 'gzip'})
            self.assertNotIn('Content-Encoding', response.headers)
            self.assertEqual(response.data, b'a' * 4096)
        response = self.client.get('/encoded', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(gzip.decompress(response.data), b'a' * 4096)

    def test_no_transform_and_head_are_respected(self):
        @self.app.get('/opaque')
        def opaque():
            return Response(b'a' * 4096, mimetype='application/json', headers={'Cache-Control': 'no-transform'})

        response = self.client.get('/opaque', headers={'Accept-Encoding': 'gzip'})
        self.assertNotIn('Content-Encoding', response.headers)
        self.assertNotIn('Content-Encoding', self.client.head('/api/example', headers={'Accept-Encoding': 'gzip'}).headers)


if __name__ == '__main__':
    unittest.main()
