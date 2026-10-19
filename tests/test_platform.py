import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import app_db
import agent
import rag
import server
from server import FileHandler


class PlatformPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(app_db, 'DB_PATH', self.root / 'private' / 'platform.sqlite3')
        self.db_patch.start()
        app_db.init()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_agentic_chat_records_real_request_milestones_without_prompt_text(self):
        owner = app_db.new_owner_id()
        previous = app_db.create_conversation(owner, title='Salesforce notes', model='qwen35-08b-q4_0')
        app_db.add_message(owner, previous['id'], 'user', 'Explain Salesforce Cloud Computing.', 'qwen35-08b-q4_0')
        app_db.add_message(owner, previous['id'], 'assistant', 'A prior document discussion.', 'qwen35-08b-q4_0')
        conversation = app_db.create_conversation(owner, model='qwen35-08b-q4_0')
        observed_context = []
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        try:
            body = json.dumps({'owner':owner,'conversationId':conversation['id'],'question':'secret prompt text','provider':'local','model':'qwen35-08b-q4_0','selected':[],'requestId':'a'*32}).encode()
            req = Request(base+'/api/agentic/ask', data=body, method='POST', headers={'Authorization':token,'Content-Type':'application/json'})
            def fake_chat(*args, activity=None, **kwargs):
                observed_context.append({'selected':args[1], 'history':args[2]})
                activity('Runtime accepted the inference request', source='local-ai', stage='inference', level='INFO', state='working', metadata={'streaming': True})
                return {'answer':'A safe test answer.','sources':[],'model':'qwen35-08b-q4_0'}
            with patch('local_ai.chat', side_effect=fake_chat):
                with urlopen(req, timeout=5) as response:
                    result=json.loads(response.read())
                    self.assertEqual(result['answer'], 'A safe test answer.')
                    self.assertEqual(result['requestId'], 'a'*32)
            self.assertEqual(observed_context, [{'selected': [], 'history': []}], "A new conversation must not receive a previous conversation's history or files.")
            req = Request(base+'/api/agentic/activity?owner='+owner+'&requestId='+('a'*32), headers={'Authorization':token})
            with urlopen(req, timeout=5) as response:
                events = json.loads(response.read())['events']
            messages = [event['message'] for event in events]
            self.assertIn('Request received', messages)
            self.assertTrue(all(event['requestId']=='a'*32 for event in events))
            self.assertIn('Request schema and input validated', messages)
            self.assertIn('Runtime accepted the inference request', messages)
            self.assertTrue(any(e['stage']=='inference' and e['requestId']=='a'*32 for e in events))
            self.assertIn('Using Qwen3.5 0.8B Instruct', messages)
            self.assertIn('Request sent to local inference', messages)
            self.assertIn('Response generated successfully', messages)
            self.assertNotIn('secret prompt text', json.dumps(events))
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_agent_activity_is_authenticated_and_scoped_to_owner(self):
        import agent_activity
        owner = app_db.new_owner_id()
        other = app_db.new_owner_id()
        agent_activity.record(owner, 'Request sent to local inference', 'working')
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(base + '/api/agentic/activity?owner=' + owner, timeout=3)
            self.assertEqual(err.exception.code, 401)
            req = Request(base + '/api/agentic/activity?owner=' + owner, headers={'Authorization':token})
            with urlopen(req, timeout=3) as response:
                events = json.loads(response.read())['events']
            self.assertEqual([e['message'] for e in events], ['Request sent to local inference'])
            req = Request(base + '/api/agentic/activity?owner=' + other, headers={'Authorization':token})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read())['events'], [])
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_conversations_persist_and_are_scoped_to_owner(self):
        owner = app_db.new_owner_id()
        other = app_db.new_owner_id()
        convo = app_db.create_conversation(owner, model='local-model')
        app_db.add_message(owner, convo['id'], 'user', 'Explain indexing', 'local-model')
        app_db.add_message(owner, convo['id'], 'assistant', 'Indexing stores chunks.', 'local-model')
        loaded = app_db.get_conversation(owner, convo['id'])
        self.assertEqual(loaded['title'], 'Explain indexing')
        self.assertEqual([m['role'] for m in loaded['messages']], ['user', 'assistant'])
        self.assertIsNone(app_db.get_conversation(other, convo['id']))
        self.assertEqual(app_db.health()['messages'], 2)

    def test_cpu_metrics_do_not_report_fake_zero_when_system_counters_are_denied(self):
        import health
        with patch('health.Path.read_text', side_effect=PermissionError('restricted')):
            self.assertIsNone(health._cpu())
        self.assertIn('unavailable', health._cpu_unavailable_reason)

    def test_process_cpu_metric_uses_cpu_time_delta(self):
        import health
        from pathlib import Path
        samples = [100, 200]
        clock = [10.0, 11.0]
        def fake_read_text(path, *args, **kwargs):
            fields = ['0'] * 13
            fields[11] = str(samples.pop(0))
            fields[12] = '0'
            return '123 (test worker) ' + ' '.join(fields)
        with patch.object(Path, 'read_text', fake_read_text), patch('health.os.sysconf', return_value=100), \
             patch('health.os.cpu_count', return_value=2), patch('health.time.monotonic', side_effect=clock):
            self.assertIsNone(health._process_cpu())
            self.assertEqual(health._process_cpu(), 50.0)

    def test_agentic_memory_metrics_are_authenticated_and_truthfully_nullable(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(base + '/api/agentic/metrics/memory', timeout=3)
            self.assertEqual(err.exception.code, 401)
            req = Request(base + '/api/agentic/metrics/memory', headers={'Authorization': token})
            with urlopen(req, timeout=5) as response:
                metrics = json.loads(response.read())
            self.assertIn('timestamp', metrics)
            self.assertIn('system', metrics)
            self.assertIn('process', metrics)
            self.assertIn('capabilities', metrics)
            self.assertEqual(metrics['model']['memoryBytes'], None)
            self.assertFalse(metrics['capabilities']['modelMemory'])
            self.assertEqual(metrics['accelerator']['usedBytes'], None)
            if metrics['system']['totalBytes'] is not None and metrics['system']['availableBytes'] is not None:
                self.assertEqual(metrics['system']['usedBytes'], metrics['system']['totalBytes'] - metrics['system']['availableBytes'])
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_local_greeting_is_not_grounded_in_unrelated_indexed_files(self):
        import local_ai
        model = 'qwen25-05b-q4km'
        captured = []
        with patch('local_ai._active_model_id', return_value=model), \
             patch('local_ai._completion', side_effect=lambda messages, **kwargs: captured.extend(messages) or 'Hello! How can I help?'), \
             patch('rag.retrieve') as retrieve:
            result = local_ai._chat_impl('Hello', [], [], model)
        self.assertEqual(result['answer'], 'Hello! How can I help?')
        retrieve.assert_not_called()
        self.assertFalse(any('Salesforce' in str(message.get('content', '')) for message in captured))

    def test_substantive_local_question_still_uses_relevant_indexed_context(self):
        import local_ai
        model = 'qwen25-05b-q4km'
        captured = []
        doc = {'path':'course/salesforce.txt','name':'salesforce.txt','page':None,'text':'Salesforce Cloud Computing notes.'}
        with patch('local_ai._active_model_id', return_value=model), \
             patch('local_ai._completion', side_effect=lambda messages, **kwargs: captured.extend(messages) or 'The notes describe Salesforce Cloud Computing.'), \
             patch('rag.retrieve', return_value=[doc]) as retrieve:
            result = local_ai._chat_impl('Explain Salesforce Cloud Computing', [], [], model)
        retrieve.assert_called_once_with('Explain Salesforce Cloud Computing', limit=3, max_chars=2400)
        self.assertIn('INDEXED SOURCE: course/salesforce.txt', captured[0]['content'])
        self.assertEqual(result['sources'][0]['path'], 'course/salesforce.txt')

    def test_standalone_agentic_route_requires_auth_and_serves_full_chat_page(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(base + '/agentic', timeout=3)
            self.assertEqual(err.exception.code, 401)
            req = Request(base + '/agentic', headers={'Authorization': token})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
                page = response.read().decode()
            self.assertIn('Agentic Chat', page)
            self.assertIn('href="/"', page)
            self.assertIn('href="/agentic"', page)
            self.assertNotIn('id="agenticTab"', page)
            self.assertIn('html.agentic-route', page)
            self.assertIn('body.theme-dark .agent-privacy', page)
            self.assertIn('id="globalMenu"', page)
            self.assertNotIn('id="agenticHome"', page)
            self.assertIn('id="menuHomeLink" class="global-menu-row hidden"', page)
            self.assertIn("homeLink.classList.toggle('hidden',!onAgentic)", page)
            self.assertIn('await newAgenticConversation()', page)
            self.assertNotIn('openAgenticConversation(d.conversations[0].id)', page)
            self.assertIn('document.documentElement.classList.add("agentic-route")', page)
            self.assertIn('id="agenticActivityDetails"', page)
            self.assertIn('id="activityLogsView"', page)
            self.assertIn('id="agenticRamToggle"', page)
            self.assertIn('id="databaseTab"', page)
            self.assertIn('id="databasePanel"', page)
            self.assertIn('id="localAiOptimize"', page)
            self.assertIn('id="localAiActionProgress"', page)
            self.assertIn('id="localAiActivateSpinner"', page)
            self.assertIn('id="localAiActionLog"', page)
            self.assertIn('localModelLoadGeneration', page)
            self.assertIn('generation!==localModelLoadGeneration', page)
            self.assertIn('localModelLoadConfirmed=true', page)
            self.assertIn('Backend confirms the selected model is loaded and responding', page)
            self.assertIn('id="healthOptimize"', page)
            self.assertIn('id="agenticRecovery"', page)
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_database_explorer_authenticates_paginates_searches_and_is_read_only(self):
        owner = app_db.new_owner_id()
        conversation = app_db.create_conversation(owner, title='SQLite explorer test', model='local')
        app_db.add_message(owner, conversation['id'], 'user', 'searchable memory test', 'local')
        app_db.add_message(owner, conversation['id'], 'assistant', 'read-only response', 'local')
        import db_explorer
        overview = db_explorer.overview()
        self.assertEqual(overview['type'], 'SQLite')
        self.assertTrue(overview['readOnly'])
        self.assertIn('messages', {item['name'] for item in overview['tables']})
        messages = db_explorer.table('messages', page=1, limit=1, search='searchable', sort='role')
        self.assertEqual(messages['total'], 1)
        self.assertEqual(messages['records'][0]['content'], 'searchable memory test')
        with app_db._connect() as connection:
            connection.execute('CREATE TABLE private_test (id INTEGER PRIMARY KEY, access_token TEXT)')
            connection.execute('INSERT INTO private_test (access_token) VALUES (?)', ('sample-secret-token-value',))
        private_record = db_explorer.table('private_test')['records'][0]
        self.assertEqual(private_record['access_token'], '[REDACTED]')
        with db_explorer._connect() as readonly:
            with self.assertRaises(Exception):
                readonly.execute('DELETE FROM messages')

        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(base + '/api/database/overview', timeout=3)
            self.assertEqual(err.exception.code, 401)
            req = Request(base + '/api/database/overview', headers={'Authorization': token})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read())['state'], 'healthy')
            from urllib.parse import urlencode
            req = Request(base + '/api/database/table?' + urlencode({'name':'messages','search':'read-only','page':1,'limit':10}), headers={'Authorization': token})
            with urlopen(req, timeout=3) as response:
                result = json.loads(response.read())
                self.assertEqual(result['total'], 1)
                self.assertEqual(result['records'][0]['role'], 'assistant')
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)

    def test_database_explorer_rejects_untrusted_table_identifiers(self):
        import db_explorer
        with self.assertRaisesRegex(ValueError, 'Table not found'):
            db_explorer.table('messages; DROP TABLE conversations')

    def test_existing_authenticated_file_upload_and_download_still_work(self):
        shared = self.root / 'public-shared'
        shared.mkdir()
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
        base = f'http://127.0.0.1:{http.server_port}'
        try:
            with patch.object(server, 'ROOT', shared):
                upload = Request(base + '/api/upload?path=&name=smoke.txt', data=b'preserved file operation', method='PUT',
                                 headers={'Authorization':token,'Content-Type':'application/octet-stream'})
                with urlopen(upload, timeout=3) as response:self.assertEqual(response.status,201)
                download = Request(base + '/download?path=smoke.txt', headers={'Authorization':token})
                with urlopen(download, timeout=3) as response:self.assertEqual(response.read(),b'preserved file operation')
        finally:
            http.shutdown();http.server_close();thread.join(timeout=2)

    def test_index_job_state_is_persisted_for_restart_recovery(self):
        job = {'id':'a' * 32,'status':'running','total':2,'done':1,'current':'notes.txt',
               'results':[{'path':'first.txt','status':'indexed'}],'startedAt':time.time()}
        app_db.save_index_job(job)
        recovered = app_db.last_index_job()
        self.assertTrue(recovered['running'])
        self.assertEqual(recovered['done'], 1)
        self.assertEqual(recovered['current'], 'notes.txt')
        self.assertEqual(recovered['results'][0]['path'], 'first.txt')

    def test_local_chunk_retrieval_and_failed_reindex_retains_old_data_but_marks_stale(self):
        path = 'docs/networking.txt'
        chunks = [(None, 'Multicast DNS resolves names on a local network.')]
        first = app_db.upsert_document(path, 'networking.txt', 'a' * 64, 50, 1.0, chunks)
        self.assertFalse(first['unchanged'])
        self.assertEqual(app_db.retrieve_chunks('multicast DNS')[0]['path'], path)
        failed = app_db.upsert_document(path, 'networking.txt', 'b' * 64, 51, 2.0, [], 'temporary parser error')
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['chunks'], 1)
        self.assertEqual(app_db.retrieve_chunks('multicast DNS'), [])
        self.assertEqual(app_db.indexed_documents()[0]['chunk_count'], 1)

    def test_interrupted_index_job_is_reported_as_failed_not_running_forever(self):
        import rag
        app_db.save_index_job({'id':'b' * 32,'status':'running','total':1,'done':0,'current':'huge.pdf',
                               'results':[],'startedAt':time.time()})
        fresh_state={'running':False,'id':'','total':0,'done':0,'current':'','results':[],'startedAt':None,'error':''}
        with patch.object(rag,'_job',fresh_state):
            result=rag.status()
        self.assertFalse(result['job']['running'])
        self.assertEqual(result['job']['status'],'failed')
        self.assertIn('server restarted',result['job']['error'])

    def test_index_job_uses_shared_root_and_reports_progress(self):
        shared = self.root / 'shared'
        shared.mkdir()
        (shared / 'notes.txt').write_text('Local retrieval is bounded and stored in SQLite.', encoding='utf-8')
        with patch.object(agent, 'ROOT', shared):
            started = rag.start(['notes.txt'])
            for _ in range(100):
                state = rag.status()
                if not state['job']['running']:
                    break
                time.sleep(.02)
            self.assertEqual(started['total'], 1)
            self.assertEqual(state['job']['results'][0]['status'], 'indexed')
            self.assertEqual(rag.retrieve('bounded SQLite')[0]['path'], 'notes.txt')

    def test_conversation_and_health_routes_require_existing_auth(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        http.password = 'unit-test-password'
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{http.server_port}'
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(base + '/api/health', timeout=3)
            self.assertEqual(err.exception.code, 401)
            token = 'Basic ' + base64.b64encode(b'admin:unit-test-password').decode()
            req = Request(base + '/api/health/live', headers={'Authorization': token})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read())['state'], 'alive')
            req = Request(base + '/api/health', headers={'Authorization': token})
            with urlopen(req, timeout=5) as response:
                health = json.loads(response.read())
            self.assertIn('resources', health)
            self.assertIn('database', health)
            owner = app_db.new_owner_id()
            req = Request(base + '/api/conversations', data=json.dumps({'owner': owner, 'title': 'Persistent chat'}).encode(),
                          headers={'Authorization': token, 'Content-Type': 'application/json'})
            with urlopen(req, timeout=3) as response:
                conversation = json.loads(response.read())
            req = Request(base + '/api/conversations?owner=' + owner, headers={'Authorization': token})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read())['conversations'][0]['id'], conversation['id'])
            ask_body = {'owner': owner, 'conversationId': conversation['id'], 'provider': 'openrouter',
                        'model': 'openrouter/free', 'question': 'Find an indexed point', 'selected': []}
            req = Request(base + '/api/agentic/ask', data=json.dumps(ask_body).encode(),
                          headers={'Authorization': token, 'Content-Type': 'application/json'})
            with patch.object(agent, 'ask', return_value={'answer':'Grounded response','sources':[], 'indexWarnings':[]}):
                with urlopen(req, timeout=3) as response:
                    self.assertEqual(json.loads(response.read())['answer'], 'Grounded response')
            saved = app_db.get_conversation(owner, conversation['id'])
            self.assertEqual([m['role'] for m in saved['messages']], ['user', 'assistant'])
            self.assertEqual(saved['messages'][-1]['content'], 'Grounded response')

            long_body = {'owner': owner, 'conversationId': conversation['id'], 'provider': 'openrouter',
                         'model': 'openrouter/free', 'question': 'Return a long response', 'selected': []}
            long_req = Request(base + '/api/agentic/ask', data=json.dumps(long_body).encode(),
                               headers={'Authorization': token, 'Content-Type': 'application/json'})
            with patch.object(agent, 'ask', return_value={'answer': 'A' * 35000, 'sources': []}):
                with urlopen(long_req, timeout=3) as response:
                    bounded = json.loads(response.read())['answer']
            self.assertEqual(len(bounded), app_db.MAX_MESSAGE)
            self.assertTrue(bounded.endswith('conversation history limit.]'))
            saved = app_db.get_conversation(owner, conversation['id'])
            self.assertEqual(saved['messages'][-1]['content'], bounded)
        finally:
            http.shutdown(); http.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
