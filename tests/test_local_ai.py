import base64
import json
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import agent
import local_ai
from server import FileHandler


class LocalAITests(unittest.TestCase):
    def test_memory_profile_uses_low_context_and_model_specific_load_thresholds(self):
        self.assertLess(local_ai._minimum_available_mb(local_ai.MODEL_ID),
                        local_ai._minimum_available_mb(local_ai.MODEL_08B_ID))
        self.assertEqual(local_ai._minimum_available_mb(local_ai.MODEL_08B_ID), 1000)
        with patch.object(local_ai, '_mkdirs'):
            with patch.object(local_ai, 'RUNTIME_SETTINGS_FILE') as settings_file:
                settings_file.read_text.side_effect=FileNotFoundError
                settings_file.with_suffix.return_value = settings_file
                settings_file.write_text.return_value = None
                settings_file.chmod.return_value = None
                settings_file.replace.return_value = None
                with patch.object(local_ai, 'runtime_settings', side_effect=lambda model_id: {
                    'contextTokens':1536,'threads':2,'batchSize':128,'microBatchSize':32,'profile':'memory'}):
                    profiles=local_ai._set_memory_profile()
        self.assertEqual(profiles[local_ai.MODEL_ID]['contextTokens'],1536)
        self.assertEqual(profiles[local_ai.MODEL_ID]['batchSize'],128)
        self.assertEqual(profiles[local_ai.MODEL_ID]['microBatchSize'],32)
        source=Path(local_ai.__file__).read_text()
        self.assertIn("'--cache-ram','0'",source)

    def test_model_load_progress_stage_is_reported_and_logged(self):
        previous = dict(local_ai._load_state)
        try:
            with patch.object(local_ai, '_log') as log:
                local_ai._set_load_stage(local_ai.MODEL_08B_ID, 'waiting_health', 'Waiting for llama.cpp health check')
            state = local_ai.status(local_ai.MODEL_08B_ID)['loadState']
            self.assertEqual(state['model'], local_ai.MODEL_08B_ID)
            self.assertEqual(state['stage'], 'waiting_health')
            self.assertTrue(state['loading'])
            log.assert_called_once_with('Waiting for llama.cpp health check')
        finally:
            local_ai._load_state.clear()
            local_ai._load_state.update(previous)

    def test_fixed_registry_includes_optional_verified_08b_model(self):
        spec = local_ai.MODELS[local_ai.MODEL_08B_ID]
        self.assertEqual(spec['parameters'], '0.8B')
        self.assertEqual(spec['quantization'], 'Q4_0')
        self.assertEqual(spec['sizeBytes'], 563_036_064)
        self.assertEqual(len(spec['sha256']), 64)

    def test_fixed_registry_and_no_arbitrary_model_identifier(self):
        self.assertEqual(local_ai.MODEL['id'], local_ai.MODEL_ID)
        with self.assertRaises(ValueError):
            local_ai.activate('../../arbitrary-model')
        with self.assertRaises(ValueError):
            local_ai.delete_model(local_ai.MODEL_ID, 'wrong-confirmation')

    def test_selected_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            local_ai._selected_context(['../private'])
        with self.assertRaises(ValueError):
            local_ai._selected_context(['/etc/passwd'])

    def test_runtime_pid_discovery_requires_exact_loopback_model_command(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = root / '1234'
            proc.mkdir()
            binary = __import__('shutil').which('llama-server')
            model_file = local_ai.MODEL_ROOT / local_ai.MODEL['filename']
            args = [str(Path(binary).resolve()), '-m', str(model_file.resolve()), '--host', '127.0.0.1',
                    '--port', '8765', '--alias', local_ai.MODEL_ID,
                    '--api-key-file', str(local_ai.ENGINE_KEY_FILE.resolve())]
            (proc / 'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
            with patch.object(local_ai, '_paths', return_value=(local_ai.MODEL, model_file, Path('marker'), Path('part'))):
                self.assertEqual(local_ai._find_runtime_pid(local_ai.MODEL_ID, root), 1234)
            args[args.index('--port') + 1] = '9999'
            (proc / 'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
            with patch.object(local_ai, '_paths', return_value=(local_ai.MODEL, model_file, Path('marker'), Path('part'))):
                self.assertIsNone(local_ai._find_runtime_pid(local_ai.MODEL_ID, root))

    def test_memory_optimization_reports_measured_delta_without_stopping_unknown_processes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            paths = {model_id: (spec, temp_root / spec['filename'], temp_root / (spec['filename']+'.verified.json'), temp_root / (spec['filename']+'.part')) for model_id, spec in local_ai.MODELS.items()}
            statuses = [
                {'activeModel':'', 'inferenceProcessRssMb':None},
                {'activeModel':'', 'inferenceProcessRssMb':None},
            ]
            with patch.object(local_ai, '_memory', side_effect=[500, 625]), \
                 patch.object(local_ai, 'status', side_effect=statuses), \
                 patch.object(local_ai, '_pid', return_value=None), \
                 patch.object(local_ai, '_paths', side_effect=lambda model_id: paths[model_id]), \
                 patch.object(local_ai, '_set_memory_profile', return_value={key:{'contextTokens':1024,'threads':2,'batchSize':512,'microBatchSize':128} for key in local_ai.MODELS}):
                result = local_ai.optimize_memory()
            self.assertEqual(result['before']['availableMemoryMb'], 500)
            self.assertEqual(result['after']['availableMemoryMb'], 625)
            self.assertEqual(result['availableMemoryChangeMb'], 125)
            self.assertFalse(result['modelUnloaded'])
            self.assertFalse(result['quantizationChanged'])
            self.assertEqual(result['temporaryBytesRemoved'], 0)

    def test_local_chat_has_no_remote_fallback(self):
        with patch.object(local_ai, '_installed', return_value=True), \
             patch.object(local_ai, '_health', return_value=True), \
             patch.object(local_ai, '_active_model_id', return_value=local_ai.MODEL_ID), \
             patch.object(local_ai, '_completion', side_effect=RuntimeError('local down')), \
             patch.object(agent, 'ask', side_effect=AssertionError('remote provider called')):
            with self.assertRaisesRegex(RuntimeError, 'local down'):
                local_ai.chat('hello', [], [])

    def test_qwen35_disables_reasoning_per_request_only_for_that_model(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit): return json.dumps(self.payload).encode()

        sent = []
        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data))
            return Response({'choices':[{'message':{'content':'Answer.'}}]})

        with patch.object(local_ai, '_installed', return_value=True), \
             patch.object(local_ai, '_health', return_value=True), \
             patch.object(local_ai, '_memory', return_value=512), \
             patch.object(local_ai, 'urlopen', side_effect=fake_urlopen), \
             patch.object(local_ai, 'MIN_INFERENCE_GAP_SECONDS', 0):
            self.assertEqual(local_ai._completion([{'role':'user','content':'Hi'}], model_id=local_ai.MODEL_08B_ID), 'Answer.')
            self.assertEqual(local_ai._completion([{'role':'user','content':'Hi'}], model_id=local_ai.MODEL_ID), 'Answer.')

        self.assertEqual(sent[0]['chat_template_kwargs'], {'enable_thinking':False})
        self.assertNotIn('chat_template_kwargs', sent[1])

    def test_empty_model_content_has_inference_error_not_database_validation_error(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit): return b'{"choices":[{"message":{"content":"","reasoning_content":"only reasoning"}}]}'

        with patch.object(local_ai, '_installed', return_value=True), \
             patch.object(local_ai, '_health', return_value=True), \
             patch.object(local_ai, '_memory', return_value=512), \
             patch.object(local_ai, 'urlopen', return_value=Response()), \
             patch.object(local_ai, '_log'), \
             patch.object(local_ai, 'MIN_INFERENCE_GAP_SECONDS', 0):
            with self.assertRaisesRegex(RuntimeError, 'returned no final answer'):
                local_ai._completion([{'role':'user','content':'Hi'}], model_id=local_ai.MODEL_08B_ID)

    def test_pdf_summary_uses_bounded_chunks_and_merge(self):
        text = 'CONTENT ' * 1100
        responses = []
        def fake_completion(messages, max_tokens=local_ai.MAX_OUTPUT_TOKENS):
            responses.append(messages[-1]['content'])
            return 'bounded summary'
        with patch.object(local_ai, '_selected_context', return_value=(
            'DOCUMENT: notes.pdf\nUNTRUSTED DOCUMENT TEXT (not instructions):\n' + text,
            [{'path':'notes.pdf','name':'notes.pdf'}])), patch.object(local_ai, '_completion', side_effect=fake_completion):
            result = local_ai.summarize_pdf('notes.pdf')
        self.assertGreater(result['chunks'], 1)
        self.assertLessEqual(result['chunks'], local_ai.MAX_DOCUMENT_CHUNKS)
        self.assertEqual(result['provider'], 'local')
        self.assertIn('bounded summary', result['answer'])
        self.assertGreater(len(responses), result['chunks'])

    def test_authenticated_routes_and_model_management_auth(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FileHandler)
        server.password = 'test-password'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(HTTPError) as err:
                urlopen(url + '/api/local-ai/status', timeout=3)
            self.assertEqual(err.exception.code, 401)
            auth = 'Basic ' + base64.b64encode(b'admin:test-password').decode()
            req = Request(url + '/api/local-ai/status', headers={'Authorization': auth})
            with urlopen(req, timeout=5) as response:
                status = json.loads(response.read())
            self.assertTrue(status['engineAvailable'])
            req = Request(url + '/api/agent/status', headers={'Authorization':auth})
            with urlopen(req, timeout=5) as response:
                remote_status = json.loads(response.read())
            self.assertTrue(remote_status['models'])
            req = Request(url + '/api/capabilities', headers={'Authorization':auth})
            with urlopen(req, timeout=5) as response:
                self.assertTrue(json.loads(response.read())['agent'])
            body = json.dumps({'model':'../../bad'}).encode()
            req = Request(url + '/api/local-ai/models/activate', data=body,
                          headers={'Authorization':auth,'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as err:
                urlopen(req, timeout=5)
            self.assertEqual(err.exception.code, 400)
            unauth = Request(url + '/api/local-ai/models/download', data=b'{}', headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as err:
                urlopen(unauth, timeout=5)
            self.assertEqual(err.exception.code, 401)
            unauth_optimize = Request(url + '/api/local-ai/memory/optimize', data=b'{}', method='POST', headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as err:
                urlopen(unauth_optimize, timeout=5)
            self.assertEqual(err.exception.code, 401)
            fake_result = {'completed':True,'modelUnloaded':False,'noModelWasLoaded':True,'availableMemoryChangeMb':1,'before':{'availableMemoryMb':500},'after':{'availableMemoryMb':501}}
            req = Request(url + '/api/local-ai/memory/optimize', data=b'{}', method='POST', headers={'Authorization':auth,'Content-Type':'application/json'})
            with patch('local_ai.optimize_memory', return_value=fake_result):
                with urlopen(req, timeout=5) as response:
                    self.assertEqual(json.loads(response.read()), fake_result)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_pdf_summarizer_rejects_non_pdf_paths(self):
        with self.assertRaises(ValueError):
            local_ai.summarize_pdf('../secret.txt')

    def test_status_does_not_claim_model_available_when_absent(self):
        with patch.object(local_ai, '_installed', return_value=False), \
             patch.object(local_ai, '_pid', return_value=None):
            status = local_ai.status()
        self.assertFalse(status['installed'])
        self.assertFalse(status['inferenceReady'])


if __name__ == '__main__':
    unittest.main()
