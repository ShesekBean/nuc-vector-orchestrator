#!/usr/bin/env python3
"""
HTTP API wrapper for chatgpt-query.py.

Runs on the NUC host, listens on 172.17.0.1:18792 (Docker bridge)
so OpenClaw's container can reach it via curl.

POST /query  {"message": "..."}  →  {"response": "..."}
GET  /health                     →  {"status": "ok"}
"""

import json
import os
import sys
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

SCRIPT = os.path.join(os.path.dirname(__file__), "chatgpt-query.py")
BIND_HOST = "172.17.0.1"
BIND_PORT = 18792


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/query":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else "{}"

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        message = data.get("message", "").strip()
        if not message:
            self._respond(400, {"error": "message required"})
            return

        try:
            result = subprocess.run(
                [sys.executable, SCRIPT, "--json", message],
                capture_output=True,
                text=True,
                timeout=150,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
            )

            if result.returncode != 0:
                self._respond(502, {
                    "error": "chatgpt query failed",
                    "detail": result.stderr.strip()[:500],
                })
                return

            try:
                resp_data = json.loads(result.stdout)
            except json.JSONDecodeError:
                resp_data = {"response": result.stdout.strip()}

            self._respond(200, resp_data)

        except subprocess.TimeoutExpired:
            self._respond(504, {"error": "ChatGPT query timed out"})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Quiet logging
        print(f"[chatgpt-server] {args[0]}")


def main():
    # Also bind to localhost for testing from host
    print(f"ChatGPT API server starting on {BIND_HOST}:{BIND_PORT}")
    server = HTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
