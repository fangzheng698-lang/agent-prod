#!/usr/bin/env python3
"""Mock OpenAI-compatible server for testing."""
import http.server
import json
import time


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        cl = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(cl)) if cl > 0 else {}
        msgs = body.get('messages', [])
        last = msgs[-1]['content'][:80] if msgs else 'hello'
        resp = {
            'id': 'mock-001', 'object': 'chat.completion', 'created': int(time.time()),
            'model': 'mock', 'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': f'Mock reply to: {last}'},
                'finish_reason': 'stop'
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())
    def log_message(self, *a): pass

# Use fixed port 19876
port = 19876
print(f"Mock OpenAI server starting on 127.0.0.1:{port}", flush=True)
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
