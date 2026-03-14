#!/usr/bin/env python3
"""
ChatGPT HTTP proxy with persistent browser.

Keeps a Chromium browser open between queries so responses are fast (~10-20s
instead of 2-4min). The browser launches on first request and stays alive.

POST /query  {"message": "..."}  →  {"response": "..."}
GET  /health                     →  {"status": "ok", "browser": "running"|"idle"}
"""

import json
import os
import sys
import time
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


def _clean_locks():
    """Remove stale browser lock files."""
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _ensure_browser():
    """Launch browser if not already running. Returns the page."""
    global _playwright, _context, _page

    if _page is not None:
        try:
            # Quick check that page is still alive
            _page.title()
            return _page
        except Exception:
            # Browser died, clean up
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

    # Navigate to ChatGPT and wait for it to be ready
    _page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)
    _page.wait_for_selector(
        '#prompt-textarea, .ProseMirror[contenteditable="true"]',
        timeout=20_000,
    )
    print("[browser] ChatGPT loaded, browser ready")
    return _page


def _shutdown_browser():
    """Clean up browser resources."""
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
    """Click 'New chat' to start a fresh conversation."""
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


def send_message(message):
    """Send a message to ChatGPT using the persistent browser."""
    with _browser_lock:
        page = _ensure_browser()

        # Navigate to new chat (skip if input is already visible and empty)
        input_el = page.query_selector('#prompt-textarea, .ProseMirror[contenteditable="true"]')
        if not input_el:
            _start_new_chat(page)
        else:
            # Clear any leftover text in the input
            input_el.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")

        # Find the input
        input_sel = '#prompt-textarea, .ProseMirror[contenteditable="true"]'
        try:
            input_el = page.wait_for_selector(input_sel, timeout=10_000)
        except Exception:
            input_el = page.query_selector("div[contenteditable='true']")
            if not input_el:
                raise RuntimeError("Could not find message input")

        # Count existing assistant messages
        pre_count = len(
            page.query_selector_all('[data-message-author-role="assistant"]')
        )

        # Type and send
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

        # Wait for response
        response = _wait_for_response(page, pre_count)
        return response


def _wait_for_response(page, pre_count):
    """Wait for ChatGPT to finish and extract the response text."""
    start = time.time()

    # Wait for a new assistant message
    while time.time() - start < 30:
        msgs = page.query_selector_all('[data-message-author-role="assistant"]')
        if len(msgs) > pre_count:
            break
        page.wait_for_timeout(500)

    # Wait for generation to finish (stop button disappears when done)
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

    # Extract last assistant message
    msgs = page.query_selector_all('[data-message-author-role="assistant"]')
    if not msgs:
        return "(No response received)"

    last_msg = msgs[-1]
    markdown_el = last_msg.query_selector(".markdown, .prose, [class*='markdown']")
    if markdown_el:
        return markdown_el.inner_text().strip()
    return last_msg.inner_text().strip()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            browser_status = "running" if _page is not None else "idle"
            self._respond(200, {"status": "ok", "browser": browser_status})
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
            start = time.time()
            response = send_message(message)
            elapsed = time.time() - start
            print(f"[query] {elapsed:.1f}s: {message[:60]}...")
            self._respond(200, {"response": response})
        except Exception as e:
            print(f"[error] {e}")
            # Try to recover by killing the browser
            _shutdown_browser()
            self._respond(502, {"error": str(e)[:300]})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # Suppress default HTTP logs


def main():
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    print(f"[chatgpt-server] Starting on {BIND_HOST}:{BIND_PORT}")
    print(f"[chatgpt-server] Browser will launch on first query")

    server = HTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chatgpt-server] Shutting down...")
        _shutdown_browser()
        server.shutdown()


if __name__ == "__main__":
    main()
