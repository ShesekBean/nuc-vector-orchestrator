#!/usr/bin/env python3
"""
ChatGPT HTTP proxy with persistent browser and async job queue.

Keeps a Chromium browser open between queries. Uses an async pattern
so OpenClaw's short exec timeouts don't kill the request:

POST /query  {"message": "..."}  →  {"job_id": "abc123"}        (instant)
GET  /result/abc123              →  {"status":"done","response":"..."} (poll)
GET  /health                     →  {"status": "ok", ...}
"""

import json
import os
import time
import uuid
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PROFILE_DIR = os.path.expanduser("~/.openclaw/workspace/chatgpt-browser-profile")
CHATGPT_URL = "https://chatgpt.com"
BIND_HOST = "172.17.0.1"
BIND_PORT = 18792
TIMEOUT_SECONDS = 120

# Global browser state
_browser_lock = threading.Lock()
_playwright = None
_context = None
_page = None

# Job queue for async results
_jobs = {}  # job_id -> {"status": "pending"|"done"|"error", "response": str}
_jobs_lock = threading.Lock()


def _clean_locks():
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _ensure_browser():
    global _playwright, _context, _page

    if _page is not None:
        try:
            _page.title()
            return _page
        except Exception:
            _shutdown_browser()

    os.makedirs(PROFILE_DIR, exist_ok=True)
    _clean_locks()

    from playwright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    _context = _playwright.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--ozone-platform=x11",
        ],
    )
    _page = _context.pages[0] if _context.pages else _context.new_page()

    # Security: block top-level navigation away from chatgpt.com
    # Allows subresources (CDN, fonts, APIs) but prevents the page from
    # navigating to arbitrary sites (e.g. if ChatGPT generates a link)
    _ALLOWED_DOMAINS = {"chatgpt.com", "openai.com", "oaiusercontent.com", "auth0.com"}

    def _block_navigation(route):
        req = route.request
        if req.resource_type == "document":
            from urllib.parse import urlparse
            domain = urlparse(req.url).hostname or ""
            if not any(domain.endswith(d) for d in _ALLOWED_DOMAINS):
                print(f"[security] Blocked navigation to: {req.url}")
                route.abort("blockedbyclient")
                return
        route.continue_()

    _context.route("**/*", _block_navigation)

    _page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)
    _page.wait_for_selector(
        '#prompt-textarea, .ProseMirror[contenteditable="true"]',
        timeout=20_000,
    )
    print("[browser] ChatGPT loaded, browser ready")
    return _page


def _shutdown_browser():
    global _playwright, _context, _page
    try:
        if _context:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _page = None
    _context = None
    _playwright = None


def _start_new_chat(page):
    try:
        new_chat = page.query_selector(
            'a[href="/"], button[data-testid="create-new-chat-button"], '
            'a[data-testid="create-new-chat-button"]'
        )
        if new_chat:
            new_chat.click()
            page.wait_for_selector(
                '#prompt-textarea, .ProseMirror[contenteditable="true"]',
                timeout=5_000,
            )
            return
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=10_000)
        page.wait_for_selector(
            '#prompt-textarea, .ProseMirror[contenteditable="true"]',
            timeout=5_000,
        )
    except Exception:
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=10_000)
        page.wait_for_timeout(2000)


def _send_message_impl(message):
    """Send a message to ChatGPT using the persistent browser."""
    with _browser_lock:
        page = _ensure_browser()

        # Navigate to new chat (skip if input already visible)
        input_el = page.query_selector('#prompt-textarea, .ProseMirror[contenteditable="true"]')
        if not input_el:
            _start_new_chat(page)
        else:
            input_el.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")

        input_sel = '#prompt-textarea, .ProseMirror[contenteditable="true"]'
        try:
            input_el = page.wait_for_selector(input_sel, timeout=10_000)
        except Exception:
            input_el = page.query_selector("div[contenteditable='true']")
            if not input_el:
                raise RuntimeError("Could not find message input")

        pre_count = len(
            page.query_selector_all('[data-message-author-role="assistant"]')
        )

        input_el.click()
        page.keyboard.type(message, delay=5)
        page.wait_for_timeout(300)

        send_btn = page.query_selector(
            'button[data-testid="send-button"], button[aria-label="Send prompt"]'
        )
        if send_btn and send_btn.is_enabled():
            send_btn.click()
        else:
            page.keyboard.press("Enter")

        return _wait_for_response(page, pre_count)


def _wait_for_response(page, pre_count):
    start = time.time()

    while time.time() - start < 30:
        msgs = page.query_selector_all('[data-message-author-role="assistant"]')
        if len(msgs) > pre_count:
            break
        page.wait_for_timeout(500)

    while time.time() - start < TIMEOUT_SECONDS:
        stop_btn = page.query_selector(
            'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
        )
        if not stop_btn:
            page.wait_for_timeout(500)
            stop_btn = page.query_selector(
                'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
            )
            if not stop_btn:
                break
        page.wait_for_timeout(300)

    msgs = page.query_selector_all('[data-message-author-role="assistant"]')
    if not msgs:
        return "(No response received)"

    last_msg = msgs[-1]
    markdown_el = last_msg.query_selector(".markdown, .prose, [class*='markdown']")
    if markdown_el:
        return markdown_el.inner_text().strip()
    return last_msg.inner_text().strip()


def _run_job(job_id, message):
    """Run a ChatGPT query in a background thread."""
    try:
        start = time.time()
        response = _send_message_impl(message)
        elapsed = time.time() - start
        print(f"[query] {elapsed:.1f}s: {message[:60]}...")
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "response": response}
    except Exception as e:
        print(f"[error] {e}")
        _shutdown_browser()
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "response": str(e)[:300]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            browser_status = "running" if _page is not None else "idle"
            with _jobs_lock:
                pending = sum(1 for j in _jobs.values() if j["status"] == "pending")
            self._respond(200, {
                "status": "ok",
                "browser": browser_status,
                "pending_jobs": pending,
            })
        elif self.path.startswith("/result/"):
            job_id = self.path[8:]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                self._respond(404, {"error": "job not found"})
            elif job["status"] == "pending":
                self._respond(202, {"status": "pending", "message": "Still working..."})
            else:
                self._respond(200, job)
                # Clean up old jobs
                with _jobs_lock:
                    if job_id in _jobs and _jobs[job_id]["status"] != "pending":
                        del _jobs[job_id]
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

        # Security: only accept from Docker bridge (OpenClaw) or localhost
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "172.17.0.1", "172.17.0.2", "::1"):
            print(f"[security] Rejected query from {client_ip}")
            self._respond(403, {"error": "forbidden"})
            return

        # Security: block prompt injection patterns
        _blocked = [
            "ignore previous", "ignore above", "disregard",
            "new instructions", "system prompt", "you are now",
            "forget everything", "override", "jailbreak",
        ]
        msg_lower = message.lower()
        if any(pattern in msg_lower for pattern in _blocked):
            print(f"[security] Blocked suspicious message: {message[:80]}")
            self._respond(400, {"error": "message blocked by security filter"})
            return

        # Security: cap message length
        if len(message) > 2000:
            self._respond(400, {"error": "message too long (max 2000 chars)"})
            return

        # Create job and run in background
        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {"status": "pending", "response": ""}

        thread = threading.Thread(target=_run_job, args=(job_id, message), daemon=True)
        thread.start()

        # Return immediately with job ID
        self._respond(200, {"job_id": job_id})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # Client disconnected, ignore

    def log_message(self, fmt, *args):
        pass


def main():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    print(f"[chatgpt-server] Starting on {BIND_HOST}:{BIND_PORT}")
    print("[chatgpt-server] Async mode: POST /query returns job_id, GET /result/<id> to poll")

    # Bind only to Docker bridge — not exposed to LAN
    server = HTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chatgpt-server] Shutting down...")
        _shutdown_browser()
        server.shutdown()


if __name__ == "__main__":
    main()
