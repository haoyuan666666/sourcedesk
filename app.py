"""SourceDesk, based on the original DeepSeek kb-qa. Local-only Flask app."""
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from core import split_chunks, retrieve, validate_answer, call_provider

ROOT = Path(__file__).resolve().parent


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Problem(f'{label}须为1至{limit}字的非空文字。')
    return value.strip()


def create_app(config=None):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config.update(DATABASE=str(ROOT / 'data' / 'kb.db'), MAX_CONTENT_LENGTH=500000,
                      TOKEN=secrets.token_urlsafe(32), PROVIDER=None)
    if config:
        app.config.update(config)
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    ask_lock = threading.Lock()

    @contextmanager
    def db():
        conn = sqlite3.connect(app.config['DATABASE'], timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    with db() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,created_at TEXT);
            CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER NOT NULL,chunk_index INTEGER,chunk_text TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS chunks_document ON chunks(doc_id);
            CREATE TABLE IF NOT EXISTS document_originals (doc_id INTEGER PRIMARY KEY,content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS answer_history (id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,payload TEXT NOT NULL);''')

    @app.before_request
    def local_boundary():
        if not re.fullmatch(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?', request.host):
            raise Problem('仅允许本机访问。', 403)
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            raise Problem('不接受跨站请求。', 403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise Problem('不接受跨站请求。', 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-SourceDesk-Token', '')
            if not secrets.compare_digest(supplied, app.config['TOKEN']):
                raise Problem('页面凭据失效，请刷新页面。', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Problem)
    def problem(exc): return jsonify(error=exc.message), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc): return jsonify(error='请求过大或格式不正确。' if exc.code in (400, 413, 415) else '请求地址不存在或方法不支持。'), exc.code

    @app.errorhandler(Exception)
    def internal_error(exc): return jsonify(error='本地处理失败，请检查磁盘和服务状态后重试。'), 500

    def body():
        data = request.get_json()
        if not isinstance(data, dict): raise Problem('请提交对象格式的数据。')
        return data

    def sources_for(question):
        with db() as conn:
            rows = [dict(row) for row in conn.execute('SELECT c.id,c.doc_id,c.chunk_index,c.chunk_text,d.title FROM chunks c JOIN documents d ON d.id=c.doc_id')]
        return retrieve(rows, question)

    def configured(): return bool(app.config['PROVIDER'] or os.environ.get('DEEPSEEK_API_KEY', '').strip())

    @app.get('/')
    def index(): return render_template('index.html')

    @app.get('/api/bootstrap')
    def bootstrap(): return jsonify(token=app.config['TOKEN'], configured=configured(), model='injected-test-provider' if app.config['PROVIDER'] else 'deepseek-chat')

    @app.get('/api/documents')
    def documents():
        with db() as conn:
            rows = conn.execute('SELECT d.id,d.title,d.created_at,COUNT(c.id) chunk_count FROM documents d LEFT JOIN chunks c ON c.doc_id=d.id GROUP BY d.id ORDER BY d.id DESC')
            return jsonify([dict(row) for row in rows])

    @app.post('/api/documents')
    def add_document():
        data = body()
        title = text(data.get('title'), '标题', 120)
        text(data.get('content'), '资料', 100000)
        content = data['content']
        parts = split_chunks(content)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            if count + len(parts) > 20000: raise Problem('已达到2万个片段上限，请先导出和整理资料。', 409)
            doc_id = conn.execute('INSERT INTO documents(title,created_at) VALUES(?,?)', (title, datetime.now(timezone.utc).isoformat())).lastrowid
            conn.execute('INSERT INTO document_originals VALUES(?,?)', (doc_id, content))
            conn.executemany('INSERT INTO chunks(doc_id,chunk_index,chunk_text) VALUES(?,?,?)', [(doc_id, i, chunk) for i, chunk in enumerate(parts, 1)])
        return jsonify(id=doc_id, chunk_count=len(parts)), 201

    @app.get('/api/documents/<int:doc_id>')
    def document(doc_id):
        with db() as conn:
            row = conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if row is None: raise Problem('资料不存在。', 404)
            chunks = [dict(item) for item in conn.execute('SELECT * FROM chunks WHERE doc_id=? ORDER BY chunk_index,id', (doc_id,))]
            original = conn.execute('SELECT content FROM document_originals WHERE doc_id=?', (doc_id,)).fetchone()
        return jsonify(**dict(row), chunks=chunks, content=original[0] if original else '\n\n'.join(c['chunk_text'] for c in chunks), reconstructed=original is None)

    @app.delete('/api/documents/<int:doc_id>')
    def delete_document(doc_id):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone(): raise Problem('资料不存在。', 404)
            conn.execute('DELETE FROM chunks WHERE doc_id=?', (doc_id,))
            conn.execute('DELETE FROM document_originals WHERE doc_id=?', (doc_id,))
            conn.execute('DELETE FROM documents WHERE id=?', (doc_id,))
        return jsonify(success=True)

    @app.post('/api/search')
    def search():
        question = text(body().get('question'), '问题', 1000)
        return jsonify(sources=sources_for(question))

    @app.post('/api/ask')
    def ask():
        question = text(body().get('question'), '问题', 1000)
        if not configured(): raise Problem('未配置 DEEPSEEK_API_KEY，可先导入资料与检索。')
        if not ask_lock.acquire(blocking=False): raise Problem('已有回答正在生成，请等待完成。', 409)
        try:
            sources = sources_for(question)
            if sources:
                try:
                    raw = (app.config['PROVIDER'] or call_provider)(question, sources)
                    answer = validate_answer(raw, sources)
                    usage = raw.get('usage', {})
                    answer['usage'] = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                       if isinstance(usage, dict) and type(usage.get(key)) is int and 0 <= usage[key] <= 10000000}
                    answer['model'] = 'deepseek-chat' if not app.config['PROVIDER'] else 'injected-test-provider'
                except Exception:
                    raise Problem('模型请求失败，或回答引用未通过核对。请检查网络、额度和资料后手动重试；没有自动重试。', 502) from None
            else:
                answer = {'answerable': False, 'claims': [], 'answer': '资料中没有足够依据回答这个问题。', 'usage': {}, 'model': None}
            answer.update(question=question, sources=sources, created_at=datetime.now(timezone.utc).isoformat())
            with db() as conn:
                answer['id'] = conn.execute('INSERT INTO answer_history(created_at,payload) VALUES(?,?)',
                                            (answer['created_at'], json.dumps(answer, ensure_ascii=False))).lastrowid
            return jsonify(answer)
        finally:
            ask_lock.release()

    def history_record(record_id):
        with db() as conn:
            row = conn.execute('SELECT payload FROM answer_history WHERE id=?', (record_id,)).fetchone()
        if row is None: raise Problem('记录不存在。', 404)
        return {**json.loads(row[0]), 'id': record_id}

    @app.get('/api/history')
    def history():
        with db() as conn:
            records = [dict(row) for row in conn.execute('SELECT id,created_at,payload FROM answer_history ORDER BY id DESC LIMIT 100')]
        return jsonify([{'id': r['id'], 'created_at': r['created_at'], 'question': json.loads(r['payload'])['question']} for r in records])

    @app.get('/api/history/<int:record_id>')
    def history_detail(record_id): return jsonify(history_record(record_id))

    @app.get('/api/history/<int:record_id>/export')
    def export(record_id):
        data = history_record(record_id)
        fmt = request.args.get('format', 'json')
        if fmt == 'json':
            content, mime = json.dumps(data, ensure_ascii=False, indent=2), 'application/json'
        elif fmt == 'md':
            def escaped(value):
                return re.sub(r'([\\`*_{}\[\]()#+.!|<>~-])', r'\\\1', str(value))
            lines = ['# 资料问答', '', escaped(data['question']), '', escaped(data['answer']), '', '## 引用与原文', '']
            for claim in data['claims']:
                lines += [f"片段 {claim['chunk_id']}：{escaped(claim['quote'])}", '']
            for source in data['sources']:
                lines += [f"### 片段 {source['id']} · {escaped(source['title'])}", '', escaped(source['chunk_text']), '']
            lines += ['引用存在不代表语义一定正确，请对照原文判断。', '', f"模型：{data['model'] or '未调用'}；用量：{json.dumps(data['usage'])}"]
            content, mime = '\n'.join(lines), 'text/markdown'
        else: raise Problem('仅支持 JSON 或 Markdown。')
        return Response(content, content_type=mime + '; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="sourcedesk-{record_id}.{fmt}"'})

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5193, debug=False)
