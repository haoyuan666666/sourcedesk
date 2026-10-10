import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import split_chunks, retrieve, validate_answer, call_provider
from app import create_app


class CoreTests(unittest.TestCase):
    def test_chunks_keep_sentences_and_unicode(self):
        text = '报销需要发票。负责人审核后付款。\n\n' + '甲' * 1500
        chunks = split_chunks(text)
        self.assertEqual(chunks[0], '报销需要发票。负责人审核后付款。')
        self.assertTrue(all(0 < len(c) <= 700 for c in chunks))
        self.assertEqual(''.join(chunks), text.replace('\n\n', ''))

    def test_retrieval_chinese_english_and_unrelated(self):
        rows = [{'id': 1, 'title': '报销', 'chunk_text': '报销需要发票。报销提交后，财务在五个工作日内付款。'},
                {'id': 2, 'title': 'API', 'chunk_text': 'API requests have a timeout of 30 seconds.'}]
        self.assertEqual(retrieve(rows, '报销需要什么？')[0]['id'], 1)
        self.assertEqual(retrieve(rows, 'What is the API timeout?')[0]['id'], 2)
        self.assertEqual(retrieve(rows, '火星天气怎么样？'), [])
        self.assertEqual(retrieve(rows, '是什么？'), [])

    def test_exact_evidence_and_no_source_refusal(self):
        sources = [{'id': 3, 'chunk_text': '报销需要发票。'}]
        answer = {'answerable': True, 'claims': [{'text': '需要发票。', 'chunk_id': 3, 'quote': '报销需要发票。'}]}
        self.assertTrue(validate_answer(answer, sources)['answerable'])
        for key, value in [('chunk_id', 99), ('quote', '报销不需要发票。')]:
            changed = json.loads(json.dumps(answer)); changed['claims'][0][key] = value
            with self.assertRaises(ValueError): validate_answer(changed, sources)
        with self.assertRaises(ValueError): validate_answer(answer, [])
        self.assertFalse(validate_answer({'answerable': False, 'claims': []}, sources)['answerable'])

    def test_provider_protocol_and_redaction(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answerable":false,"claims":[]}'}}],
                                   'usage': {'total_tokens': 12}}).encode()
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-only'}), patch('urllib.request.urlopen', return_value=Reply()) as opened:
            result = call_provider('问题', [])
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(json.loads(request.data)['max_tokens'], 1800)
            self.assertEqual(opened.call_args.kwargs['timeout'], 45)
            self.assertEqual(result['usage']['total_tokens'], 12)
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-only'}), patch('urllib.request.urlopen', side_effect=RuntimeError('SECRET CONTENT')):
            with self.assertRaisesRegex(ValueError, '暂时失败') as exc: call_provider('问题', [])
            self.assertNotIn('SECRET', str(exc.exception))

    def test_provider_rejects_oversize_and_truncated_response(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return self.payload
        for payload in (b'x' * 262145, b'{}', b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}',
                        b'{"choices":[{"message":{"content":"{\\"answerable\\":false,\\"claims\\":[]}"}}]}'):
            reply = Reply(); reply.payload = payload
            with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-only'}), patch('urllib.request.urlopen', return_value=reply) as opened:
                with self.assertRaises(ValueError): call_provider('question', [])
                self.assertEqual(opened.call_count, 1)

    def test_refusal_discards_uncited_answer_and_boolean_ids(self):
        self.assertEqual(validate_answer({'answerable': False, 'claims': [{'text': 'invented'}], 'answer': 'invented'}, [])['claims'], [])
        for raw in ([], {}, {'answerable': True, 'claims': []}, {'answerable': True, 'claims': [{'text': 'a', 'chunk_id': True, 'quote': 'a'}]}):
            with self.assertRaises(ValueError): validate_answer(raw, [{'id': 1, 'chunk_text': 'a'}])


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'kb.db')
        self.calls = []
        def provider(question, sources):
            self.calls.append(question)
            return {'answerable': True, 'claims': [{'text': '报销需要发票。', 'chunk_id': sources[0]['id'], 'quote': '报销需要发票。'}],
                    'model': 'test-provider', 'usage': {'total_tokens': 9}}
        self.app = create_app({'TESTING': True, 'DATABASE': self.path, 'PROVIDER': provider})
        self.client = self.app.test_client()
        self.headers = {'X-SourceDesk-Token': self.client.get('/api/bootstrap').get_json()['token']}

    def tearDown(self): self.tmp.cleanup()

    def post(self, url, value): return self.client.post(url, json=value, headers=self.headers)

    def add(self):
        return self.post('/api/documents', {'title': '虚构报销规则', 'content': '报销需要发票。负责人审核后付款。'}).get_json()

    def test_import_retrieve_answer_history_export_delete(self):
        doc = self.add()
        self.assertEqual(self.post('/api/search', {'question': '报销需要什么？'}).status_code, 200)
        result = self.post('/api/ask', {'question': '报销需要什么？'}).get_json()
        self.assertTrue(result['answerable'], result)
        self.assertEqual(result['usage']['total_tokens'], 9)
        self.assertEqual(len(self.client.get('/api/history').get_json()), 1)
        for fmt in ('json', 'md'):
            export = self.client.get(f"/api/history/{result['id']}/export?format={fmt}")
            self.assertEqual(export.status_code, 200)
            self.assertIn('报销需要发票', export.data.decode())
        self.assertEqual(self.client.delete(f"/api/documents/{doc['id']}", headers=self.headers).status_code, 200)
        self.assertIn('报销需要发票', self.client.get(f"/api/history/{result['id']}").get_json()['sources'][0]['chunk_text'])

    def test_unrelated_refuses_without_provider(self):
        self.add()
        result = self.post('/api/ask', {'question': '火星天气怎么样？'}).get_json()
        self.assertFalse(result['answerable']); self.assertEqual(self.calls, [])

    def test_retrieved_context_can_still_be_insufficient_for_model(self):
        self.add()
        calls = []
        def provider(question, sources):
            calls.append(sources)
            return {'answerable': False, 'claims': [], 'usage': {'total_tokens': 13}}
        self.app.config['PROVIDER'] = provider
        response = self.post('/api/ask', {'question': '报销需要哪种发票？'})
        self.assertEqual(response.status_code, 200)
        answer = response.get_json()
        self.assertEqual(len(calls), 1)
        self.assertTrue(answer['sources'])
        self.assertFalse(answer['answerable'])
        self.assertEqual(answer['claims'], [])
        self.assertIn('没有足够依据', answer['answer'])
        self.assertEqual(answer['usage']['total_tokens'], 13)
        self.assertFalse(self.client.get(f"/api/history/{answer['id']}").get_json()['answerable'])

    def test_boundary_and_validation(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        for asset in ('app.js', 'style.css', 'favicon.svg'):
            with self.client.get('/static/' + asset) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/bootstrap').get_json()['model'], 'injected-test-provider')
        self.assertEqual(self.client.post('/api/documents', json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Host': 'evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/documents', headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.post('/api/documents', []).status_code, 400)
        self.assertEqual(self.post('/api/documents', {'title': 'x', 'content': 'a'*100001}).status_code, 400)
        self.assertEqual(self.post('/api/ask', {'question': ''}).status_code, 400)

    def test_provider_failure_is_redacted_and_not_saved_as_answer(self):
        self.add()
        self.app.config['PROVIDER'] = lambda *args: (_ for _ in ()).throw(RuntimeError('SECRET'))
        response = self.post('/api/ask', {'question': '报销需要什么？'})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('SECRET', response.data.decode())
        self.assertEqual(self.client.get('/api/history').get_json(), [])

    def test_missing_key_search_works_ask_fails(self):
        self.add(); self.app.config['PROVIDER'] = None
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''}):
            self.assertEqual(self.post('/api/search', {'question': '报销需要什么？'}).status_code, 200)
            self.assertEqual(self.post('/api/ask', {'question': '报销需要什么？'}).status_code, 400)

    def test_preserves_original_text_exactly_and_duplicate_titles(self):
        original = '  开头空格\r\n\r\n报销需要发票。\n'
        first = self.post('/api/documents', {'title': 'same', 'content': original}).get_json()
        second = self.post('/api/documents', {'title': 'same', 'content': '另一个文档'}).get_json()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.client.get(f"/api/documents/{first['id']}").get_json()['content'], original)

    def test_invalid_model_quote_not_saved(self):
        self.add()
        self.app.config['PROVIDER'] = lambda question, sources: {'answerable': True, 'claims': [{'text': '错误回答', 'chunk_id': sources[0]['id'], 'quote': '不存在的原文'}]}
        self.assertEqual(self.post('/api/ask', {'question': '报销需要什么？'}).status_code, 502)
        self.assertEqual(self.client.get('/api/history').get_json(), [])

    def test_history_survives_restart_and_export_escapes_html(self):
        self.add()
        result = self.post('/api/ask', {'question': '报销需要什么？ <script>bad()</script>'}).get_json()
        fresh = create_app({'TESTING': True, 'DATABASE': self.path}).test_client()
        self.assertEqual(fresh.get(f"/api/history/{result['id']}").get_json()['question'], result['question'])
        self.assertIn('\\<script\\>', fresh.get(f"/api/history/{result['id']}/export?format=md").data.decode())
        self.assertEqual(fresh.get(f"/api/history/{result['id']}/export?format=exe").status_code, 400)

    def test_concurrent_ask_rejected_without_second_provider_call(self):
        self.add()
        original = self.app.config['PROVIDER']
        def provider(question, sources):
            self.assertEqual(self.post('/api/ask', {'question': question}).status_code, 409)
            return original(question, sources)
        self.app.config['PROVIDER'] = provider
        self.assertEqual(self.post('/api/ask', {'question': '报销需要什么？'}).status_code, 200)
        self.assertEqual(len(self.calls), 1)

    def test_legacy_schema_and_rows_are_kept(self):
        path = str(Path(self.tmp.name) / 'legacy.db')
        with sqlite3.connect(path) as conn:
            conn.executescript('CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,created_at TEXT); CREATE TABLE chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER NOT NULL,chunk_index INTEGER,chunk_text TEXT NOT NULL);')
            conn.execute("INSERT INTO documents VALUES(7,'旧资料','2020')")
            conn.execute("INSERT INTO chunks VALUES(12,7,1,'旧资料原文')")
        conn.close()
        client = create_app({'TESTING': True, 'DATABASE': path}).test_client()
        self.assertEqual(client.get('/api/documents/7').get_json()['chunks'][0]['chunk_text'], '旧资料原文')
        self.assertEqual(client.get('/api/documents').get_json()[0]['id'], 7)


if __name__ == '__main__': unittest.main()
