import base64
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server_logs
import server
from server import FileHandler


class ServerLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp.name)
        self.audit_file = self.log_dir / 'activity.log'
        self.dir_patch = patch.object(server_logs, 'LOG_DIR', self.log_dir)
        self.file_patch = patch.object(server_logs, 'AUDIT_FILE', self.audit_file)
        self.dir_patch.start(); self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop(); self.dir_patch.stop(); self.temp.cleanup()

    def test_search_terms_are_logged_but_secrets_are_redacted(self):
        server_logs.record('file.search', '192.0.2.4', search='lecture notes', result_count=3,
                           authorization='do-not-log')
        entry = server_logs.logs()['events'][-1]
        self.assertEqual(entry['details']['search'], 'lecture notes')
        self.assertNotIn('authorization', entry['details'])
        self.assertNotIn('do-not-log', self.audit_file.read_text())
        self.assertEqual(self.audit_file.stat().st_mode & 0o777, 0o600)

    def test_sensitive_search_text_is_redacted(self):
        secret_like = 'sk-or-v1-' + 'a' * 40
        server_logs.record('file.search', '192.0.2.4', search=secret_like)
        self.assertEqual(server_logs.logs()['events'][-1]['details']['search'],
                         '[redacted sensitive-looking value]')

    def test_rotation_keeps_recent_entries_bounded(self):
        with patch.object(server_logs, 'MAX_BYTES', 220):
            for index in range(8):
                server_logs.record('http.request', '192.0.2.4', route='/api/list', http_status=200,
                                   outcome=f'event-{index}')
        self.assertTrue((self.log_dir / 'activity.log.1').exists())
        self.assertLessEqual(len(server_logs.logs(500)['events']), 6)

    def test_file_search_term_is_audited_without_query_in_access_route(self):
        shared = self.log_dir / 'shared'; shared.mkdir()
        (shared / 'lecture notes.txt').write_text('private test data', encoding='utf-8')
        server.password = 'test-password'
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        auth = 'Basic ' + base64.b64encode(b'admin:test-password').decode()
        base = f'http://127.0.0.1:{http.server_port}'
        try:
            with patch.object(server, 'ROOT', shared):
                req = Request(base + '/api/search?q=lecture%20notes', headers={'Authorization': auth})
                with urlopen(req, timeout=3) as response:
                    self.assertEqual(json.loads(response.read())['items'][0]['name'], 'lecture notes.txt')
            entry = next(item for item in reversed(server_logs.logs()['events']) if item['event'] == 'file.search')
            self.assertEqual(entry['details']['search'], 'lecture notes')
            self.assertEqual(entry['details']['result_count'], 1)
            self.assertNotIn('lecture%20notes', self.audit_file.read_text())
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_component_logs_redact_query_strings_and_credentials(self):
        component = self.log_dir / 'service.log'
        component.write_text('GET /api/search?q=private%20term 200 Authorization: Bearer abcdefghijklmnop token=secret-value\n', encoding='utf-8')
        line = server_logs._component_tail(component, 'test')[0]['line']
        self.assertIn('[query omitted]', line)
        self.assertIn('[redacted]', line)
        self.assertNotIn('private%20term', line)
        self.assertNotIn('abcdefghijklmnop', line)
        self.assertNotIn('secret-value', line)

    def test_logs_endpoint_requires_existing_server_authentication(self):
        server_logs.record('file.search', '127.0.0.1', search='dsa pdf', result_count=1)
        server = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        server.password = 'test-password'
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/logs'
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(url, timeout=3)
            self.assertEqual(err.exception.code, 401)
            auth = 'Basic ' + base64.b64encode(b'admin:test-password').decode()
            with urlopen(Request(url, headers={'Authorization': auth}), timeout=3) as response:
                result = json.loads(response.read())
            self.assertEqual(result['events'][-1]['details']['search'], 'dsa pdf')
            self.assertTrue(any(item['source'] == 'supervisor' for item in result['components']))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
