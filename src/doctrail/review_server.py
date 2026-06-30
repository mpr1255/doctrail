#!/usr/bin/env python3
"""
Quick validation UI for reviewing enrichment accuracy.

Usage:
    python -m doctrail.review_server --db /path/to.db --field is_relevant --sample 50

Then open http://localhost:8765 and press Y/N to validate each item.
"""

import sqlite3
import json
import random
from pathlib import Path
from typing import Optional
import click
import html

from .db_operations import ENRICHMENT_AUDIT_TABLE, ENRICHMENTS_TABLE

# Inline HTML - no templates needed
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Doctrail Review</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e; color: #eee;
            height: 100vh; display: flex; flex-direction: column;
        }
        .header {
            background: #16213e; padding: 12px 20px;
            display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid #0f3460;
        }
        .progress { font-size: 14px; color: #888; }
        .accuracy { font-size: 18px; font-weight: bold; }
        .accuracy.good { color: #4ade80; }
        .accuracy.ok { color: #fbbf24; }
        .accuracy.bad { color: #f87171; }
        .main { flex: 1; display: flex; overflow: hidden; }
        .content-panel {
            flex: 1; padding: 20px; overflow-y: auto;
            background: #1a1a2e;
        }
        .content {
            background: #16213e; border-radius: 8px; padding: 20px;
            max-height: calc(100vh - 280px); overflow-y: auto;
            font-size: 16px; line-height: 1.8;
            white-space: normal; word-wrap: break-word;
            word-break: break-word;
        }
        .prompt-box {
            background: #0f3460; border-radius: 8px; padding: 15px;
            margin-bottom: 15px; border-left: 4px solid #4ade80;
        }
        .prompt-label {
            font-size: 11px; color: #888; text-transform: uppercase;
            margin-bottom: 8px; letter-spacing: 1px;
        }
        .prompt-text { font-size: 13px; color: #ccc; line-height: 1.5; }
        .schema-box {
            background: #1e1e3f; border-radius: 6px; padding: 10px 15px;
            margin-top: 10px; font-family: monospace; font-size: 12px;
            color: #a78bfa;
        }
        .sidebar {
            width: 320px; background: #16213e; padding: 20px;
            border-left: 1px solid #0f3460;
            display: flex; flex-direction: column;
        }
        .classification {
            text-align: center; padding: 30px;
            background: #0f3460; border-radius: 8px; margin-bottom: 20px;
        }
        .classification .label { font-size: 12px; color: #888; margin-bottom: 8px; }
        .classification .value {
            font-size: 36px; font-weight: bold; text-transform: uppercase;
        }
        .value.yes { color: #4ade80; }
        .value.no { color: #f87171; }
        .controls { margin-top: auto; }
        .btn-row { display: flex; gap: 10px; margin-bottom: 10px; }
        .btn {
            flex: 1; padding: 20px; border: none; border-radius: 8px;
            font-size: 16px; font-weight: bold; cursor: pointer;
            transition: transform 0.1s, opacity 0.1s;
        }
        .btn:hover { opacity: 0.9; }
        .btn:active { transform: scale(0.98); }
        .btn-correct { background: #22c55e; color: white; }
        .btn-wrong { background: #ef4444; color: white; }
        .btn-skip { background: #64748b; color: white; }
        .hint { text-align: center; color: #666; font-size: 12px; margin-top: 10px; }
        .flash-green { animation: flashGreen 0.3s; }
        .flash-red { animation: flashRed 0.3s; }
        @keyframes flashGreen {
            0% { background: #22c55e; }
            50% { background: #86efac; }
            100% { background: #22c55e; }
        }
        @keyframes flashRed {
            0% { background: #ef4444; }
            50% { background: #fca5a5; }
            100% { background: #ef4444; }
        }
        .done {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; height: 100%; text-align: center;
        }
        .done h1 { font-size: 48px; margin-bottom: 20px; }
        .done .score { font-size: 72px; font-weight: bold; margin-bottom: 20px; }
        .done .details { color: #888; }
        .doc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .title { font-size: 14px; color: #4ade80; font-weight: bold; }
        .enrichment-id { font-size: 11px; color: #666; font-family: monospace; }
        .content-label { font-size: 11px; color: #888; margin-bottom: 6px; }
        .content-full { background: #16213e; border-radius: 8px; padding: 20px; margin-top: 10px;
            font-size: 15px; line-height: 1.7; white-space: normal; word-break: break-word;
            max-height: 400px; overflow-y: auto; border: 1px solid #4ade80; }
        .question { font-size: 13px; color: #fbbf24; margin-bottom: 5px; }
        .expand-link { color: #60a5fa; cursor: pointer; font-size: 11px; }
        .expand-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <strong>Reviewing:</strong> <span id="field-name">FIELD</span>
        </div>
        <div class="progress">
            <span id="current">0</span> / <span id="total">0</span>
        </div>
        <div class="accuracy" id="accuracy">--</div>
    </div>

    <div class="main" id="main-content">
        <div class="content-panel">
            <div class="prompt-box">
                <div class="prompt-label">Prompt (what LLM was asked) <span class="expand-link" onclick="togglePrompt()">[show full]</span></div>
                <div class="prompt-text" id="prompt-text">Loading...</div>
                <div class="prompt-full" id="prompt-full" style="display:none; margin-top:10px; padding-top:10px; border-top:1px solid #333; font-size:11px; color:#888; max-height:300px; overflow-y:auto;"></div>
                <div class="schema-box" id="schema-box">Schema: loading...</div>
            </div>
            <div class="doc-header">
                <span class="title" id="doc-title"></span>
                <span class="enrichment-id" id="enrichment-id"></span>
            </div>
            <div class="content-label">Content shown to model (<span id="trunc-limit">2000</span> chars) <span class="expand-link" onclick="toggleContent()">[show full]</span></div>
            <div class="content" id="content"></div>
            <div class="content-full" id="content-full" style="display:none;"></div>
        </div>
        <div class="sidebar">
            <div class="classification">
                <div class="label">LLM classified as:</div>
                <div class="value" id="llm-value">--</div>
            </div>
            <div class="question" id="question">Is this classification correct?</div>
            <div class="controls">
                <div class="btn-row">
                    <button class="btn btn-correct" onclick="vote('correct')" id="btn-correct">
                        Correct (Y)
                    </button>
                    <button class="btn btn-wrong" onclick="vote('wrong')" id="btn-wrong">
                        Wrong (N)
                    </button>
                </div>
                <button class="btn btn-skip" onclick="vote('skip')" style="width:100%">
                    Skip (S)
                </button>
                <div class="hint">Keyboard: Y = correct, N = wrong, S = skip</div>
            </div>
        </div>
    </div>

    <script>
        let items = [];
        let currentIdx = 0;
        let results = { correct: 0, wrong: 0, skip: 0 };
        let promptText = '';
        let promptFull = '';
        let schemaText = '';
        let promptExpanded = false;

        async function init() {
            const resp = await fetch('/api/items');
            const data = await resp.json();
            items = data.items;
            promptText = data.prompt || '(prompt not stored in database)';
            promptFull = data.prompt_full || promptText;
            schemaText = data.schema || 'yes | no';
            document.getElementById('field-name').textContent = data.field_name;
            document.getElementById('total').textContent = items.length;
            document.getElementById('prompt-text').textContent = promptText;
            document.getElementById('prompt-full').textContent = promptFull;
            document.getElementById('schema-box').textContent = 'Valid outputs: ' + schemaText;
            if (data.truncate_limit) {
                document.getElementById('trunc-limit').textContent = data.truncate_limit;
            }
            showItem();
        }

        let contentExpanded = false;

        function togglePrompt() {
            promptExpanded = !promptExpanded;
            document.getElementById('prompt-full').style.display = promptExpanded ? 'block' : 'none';
            document.querySelectorAll('.expand-link')[0].textContent = promptExpanded ? '[hide]' : '[show full]';
        }

        function toggleContent() {
            contentExpanded = !contentExpanded;
            document.getElementById('content-full').style.display = contentExpanded ? 'block' : 'none';
            document.querySelectorAll('.expand-link')[1].textContent = contentExpanded ? '[hide full]' : '[show full]';
        }

        function showItem() {
            if (currentIdx >= items.length) {
                showDone();
                return;
            }
            const item = items[currentIdx];
            document.getElementById('current').textContent = currentIdx + 1;
            document.getElementById('content').textContent = item.content || '(no content)';
            document.getElementById('content-full').textContent = item.full_content || item.content || '(no content)';
            document.getElementById('doc-title').textContent = item.title || item.filename || '';
            document.getElementById('enrichment-id').textContent = 'id:' + item.enrichment_id;
            const valueEl = document.getElementById('llm-value');
            valueEl.textContent = item.value;
            valueEl.className = 'value ' + (item.value?.toLowerCase() === 'yes' ? 'yes' : 'no');
            // Reset content expansion on new item
            contentExpanded = false;
            document.getElementById('content-full').style.display = 'none';
            document.querySelectorAll('.expand-link')[1].textContent = '[show full]';
            updateAccuracy();
        }

        function vote(result) {
            if (currentIdx >= items.length) return;
            results[result]++;

            // Flash feedback
            const btn = document.getElementById(result === 'correct' ? 'btn-correct' : 'btn-wrong');
            btn.classList.add(result === 'correct' ? 'flash-green' : 'flash-red');
            setTimeout(() => btn.classList.remove('flash-green', 'flash-red'), 300);

            // Record result
            fetch('/api/vote', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    sha1: items[currentIdx].sha1,
                    enrichment_id: items[currentIdx].enrichment_id,
                    value: items[currentIdx].value,
                    result: result
                })
            });

            currentIdx++;
            showItem();
        }

        function updateAccuracy() {
            const total = results.correct + results.wrong;
            if (total === 0) {
                document.getElementById('accuracy').textContent = '--';
                return;
            }
            const acc = (results.correct / total * 100).toFixed(1);
            const el = document.getElementById('accuracy');
            el.textContent = acc + '% accurate';
            el.className = 'accuracy ' + (acc >= 90 ? 'good' : acc >= 70 ? 'ok' : 'bad');
        }

        function showDone() {
            const total = results.correct + results.wrong;
            const acc = total > 0 ? (results.correct / total * 100).toFixed(1) : 0;
            const accClass = acc >= 90 ? 'good' : acc >= 70 ? 'ok' : 'bad';
            document.getElementById('main-content').innerHTML = `
                <div class="done">
                    <h1>Review Complete</h1>
                    <div class="score ${accClass}" style="color: ${acc >= 90 ? '#4ade80' : acc >= 70 ? '#fbbf24' : '#f87171'}">${acc}%</div>
                    <div class="details">
                        ${results.correct} correct, ${results.wrong} wrong, ${results.skip} skipped<br>
                        out of ${items.length} items
                    </div>
                </div>
            `;
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'y' || e.key === 'Y') vote('correct');
            else if (e.key === 'n' || e.key === 'N') vote('wrong');
            else if (e.key === 's' || e.key === 'S') vote('skip');
        });

        init();
    </script>
</body>
</html>
"""


def get_prompt_info(db_path: str, field_name: str):
    """Get the prompt and schema used for this enrichment."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Try to find prompt from the audit table.
    cursor = conn.execute(f"""
        SELECT DISTINCT full_prompt, raw_json
        FROM {ENRICHMENT_AUDIT_TABLE}
        WHERE full_prompt IS NOT NULL
        LIMIT 1
    """)
    row = cursor.fetchone()

    prompt_short = None
    prompt_full = None
    schema = None

    if row and row['full_prompt']:
        prompt_full = row['full_prompt']
        # Extract just the instruction part for display
        parts = prompt_full.split('\n\n', 1)
        prompt_short = parts[0] if parts else prompt_full[:500]

    if row and row['raw_json']:
        try:
            data = json.loads(row['raw_json'])
            if isinstance(data, dict):
                schema = list(data.keys())
        except:
            pass

    # Get distinct values for this field as schema hint
    cursor = conn.execute(f"""
        SELECT DISTINCT value FROM {ENRICHMENTS_TABLE}
        WHERE field_name = ? AND value IS NOT NULL
        LIMIT 10
    """, (field_name,))
    values = [r['value'] for r in cursor.fetchall()]
    if values:
        schema = ' | '.join(values)

    conn.close()
    return prompt_short, prompt_full, schema


def ensure_human_audit_table(db_path: str):
    """Create human_audit table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS human_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrichment_id TEXT,
            sha1 TEXT NOT NULL,
            field_name TEXT NOT NULL,
            llm_value TEXT,
            human_judgment TEXT,
            correct_value TEXT,
            reviewer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(enrichment_id, reviewer)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_human_audit_sha1 ON human_audit(sha1)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_human_audit_field ON human_audit(field_name)
    """)
    conn.commit()
    conn.close()


def save_human_audit(db_path: str, sha1: str, field_name: str, enrichment_id: str,
                     llm_value: str, judgment: str, reviewer: str = None):
    """Save a human audit result."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO human_audit
        (enrichment_id, sha1, field_name, llm_value, human_judgment, reviewer, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
    """, (enrichment_id, sha1, field_name, llm_value, judgment, reviewer))
    conn.commit()
    conn.close()


def get_review_items(db_path: str, field_name: str, sample_per_class: int = 50,
                     table_name: str = 'articles', truncate_limit: int = 2000):
    """Get stratified sample of items for review."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get distinct values for this field
    cursor = conn.execute(f"""
        SELECT DISTINCT value FROM {ENRICHMENTS_TABLE}
        WHERE field_name = ? AND value IS NOT NULL
    """, (field_name,))
    values = [row['value'] for row in cursor.fetchall()]

    items = []
    for value in values:
        # Get sample for this value - try different table names
        for tbl in [table_name, 'articles', 'documents', 'literature']:
            try:
                cursor = conn.execute(f"""
                    SELECT e.enrichment_id, e.key_value, e.value, a.title, a.filename,
                           substr(a.raw_content, 1, ?) as content,
                           a.raw_content as full_content
                    FROM {ENRICHMENTS_TABLE} e
                    JOIN {tbl} a ON e.key_value = a.sha1
                    WHERE e.field_name = ? AND e.value = ?
                    ORDER BY RANDOM()
                    LIMIT ?
                """, (truncate_limit, field_name, value, sample_per_class))

                rows = cursor.fetchall()
                if rows:
                    for row in rows:
                        items.append({
                            'enrichment_id': row['enrichment_id'],
                            'sha1': row['key_value'],
                            'value': row['value'],
                            'title': row['title'],
                            'filename': row['filename'],
                            'content': row['content'],
                            'full_content': row['full_content'][:10000] if row['full_content'] else None  # Cap at 10k
                        })
                    break
            except sqlite3.OperationalError:
                continue

    # Shuffle all items together
    random.shuffle(items)
    conn.close()
    return items, truncate_limit


def run_review_server(db_path: str, field_name: str, sample_per_class: int = 50,
                      port: int = 8765, table_name: str = 'articles',
                      truncate_limit: int = 2000):
    """Run the review server (callable from CLI or directly)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    # Ensure human_audit table exists
    ensure_human_audit_table(db_path)

    # Load items and prompt info at startup
    items, trunc = get_review_items(db_path, field_name, sample_per_class, table_name, truncate_limit)
    prompt_short, prompt_full, schema = get_prompt_info(db_path, field_name)
    results = []

    click.echo(f"Loaded {len(items)} items for review")
    click.echo(f"Field: {field_name}")
    if prompt_short:
        click.echo(f"Prompt: {prompt_short[:100]}...")
    click.echo(f"Open http://localhost:{port}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress logging

        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
            elif self.path == '/api/items':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'items': items,
                    'field_name': field_name,
                    'prompt': prompt_short,
                    'prompt_full': prompt_full,
                    'schema': schema,
                    'truncate_limit': trunc
                }, ensure_ascii=False).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == '/api/vote':
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                results.append(body)

                # Save to human_audit table
                try:
                    save_human_audit(
                        db_path=db_path,
                        sha1=body.get('sha1'),
                        field_name=field_name,
                        enrichment_id=str(body.get('enrichment_id')),
                        llm_value=body.get('value'),
                        judgment=body.get('result')
                    )
                except Exception as e:
                    click.echo(f"Warning: could not save to human_audit: {e}")

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(('localhost', port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Save results on exit
        if results:
            out_file = Path(db_path).stem + f'_review_{field_name}.json'
            with open(out_file, 'w') as f:
                json.dump(results, f, indent=2)
            click.echo(f"\nSaved {len(results)} results to {out_file}")


@click.command()
@click.option('--db', required=True, help='Path to database')
@click.option('--field', required=True, help='Field name to review (e.g., is_relevant)')
@click.option('--sample', default=50, help='Sample size per class (default: 50)')
@click.option('--port', default=8765, help='Port to run server on')
@click.option('--table', default='articles', help='Table name (default: articles)')
@click.option('--truncate', default=2000, help='Content truncation limit (default: 2000)')
def main(db: str, field: str, sample: int, port: int, table: str, truncate: int):
    """Start review server for validating enrichment accuracy."""
    run_review_server(
        db_path=db,
        field_name=field,
        sample_per_class=sample,
        port=port,
        table_name=table,
        truncate_limit=truncate
    )


if __name__ == '__main__':
    main()
