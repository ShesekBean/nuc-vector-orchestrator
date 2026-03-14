#!/usr/bin/env python3
"""
ChatGPT Web query script via Playwright.

Drives a real Chromium browser to send messages to ChatGPT,
including connected tools (Jira, Slack, email, etc.).

First run: opens browser for manual login. Subsequent runs reuse the session.

Usage:
  python3 scripts/chatgpt-query.py "your question here"
  python3 scripts/chatgpt-query.py --login          # Force login flow
  python3 scripts/chatgpt-query.py --visible "test"  # Show browser window
"""

import argparse
import json
import os
import sys
import time

PROFILE_DIR = os.path.expanduser("~/.openclaw/workspace/chatgpt-browser-profile")
CHATGPT_URL = "https://chatgpt.com"
TIMEOUT_MS = 120_000  # 2 min for tool-heavy responses


def ensure_profile_dir():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # Clean stale lock files from previous crashes
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def login_flow():
    """Open visible browser for manual ChatGPT login."""
    from playwright.sync_api import sync_playwright

    ensure_profile_dir()
    print("Opening browser for ChatGPT login...")
    print("Log in, then close the browser window when done.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--ozone-platform=x11"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(CHATGPT_URL, wait_until="domcontentloaded")

        print("Waiting for you to log in... (close browser when done)")
        try:
            # Wait until browser is closed by user
            page.wait_for_event("close", timeout=300_000)
        except Exception:
            pass
        context.close()

    print("Login session saved. You can now run queries.")


def send_message(message, headless=True):
    """Send a message to ChatGPT and return the response."""
    from playwright.sync_api import sync_playwright

    ensure_profile_dir()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--ozone-platform=x11",
            ],
        )

        page = context.pages[0] if context.pages else context.new_page()

        try:
            # Navigate to ChatGPT
            page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)

            # Wait for the page to be ready - check if we're logged in
            # The composer/input area appears when logged in
            try:
                page.wait_for_selector(
                    '#composer-background, [id="prompt-textarea"], div[contenteditable="true"]',
                    timeout=15_000,
                )
            except Exception:
                # Might need login
                if page.url and "auth" in page.url:
                    print(
                        "ERROR: Not logged in. Run with --login first:\n"
                        "  python3 scripts/chatgpt-query.py --login",
                        file=sys.stderr,
                    )
                    context.close()
                    sys.exit(1)
                # Wait a bit more - page might still be loading
                page.wait_for_timeout(5000)

            # Find the message input — ChatGPT uses ProseMirror (contenteditable div)
            input_sel = '#prompt-textarea, .ProseMirror[contenteditable="true"]'
            try:
                input_el = page.wait_for_selector(input_sel, timeout=10_000)
            except Exception:
                input_el = page.query_selector("div[contenteditable='true']")
                if not input_el:
                    print(
                        "ERROR: Could not find message input. ChatGPT UI may have changed.",
                        file=sys.stderr,
                    )
                    context.close()
                    sys.exit(1)

            # ProseMirror needs click + keyboard typing (fill() doesn't work)
            input_el.click()
            page.keyboard.type(message, delay=10)
            page.wait_for_timeout(500)

            # Count existing assistant messages before sending
            pre_count = len(
                page.query_selector_all(
                    '[data-message-author-role="assistant"]'
                )
            )

            # Send - press Enter or click the send button
            send_btn = page.query_selector(
                'button[data-testid="send-button"], button[aria-label="Send prompt"]'
            )
            if send_btn and send_btn.is_enabled():
                send_btn.click()
            else:
                page.keyboard.press("Enter")

            # Wait for response to start
            page.wait_for_timeout(2000)

            # Wait for the response to complete
            # ChatGPT shows a stop button while generating, which disappears when done
            response_text = _wait_for_response(page, pre_count)

            context.close()
            return response_text

        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            context.close()
            sys.exit(1)


def _wait_for_response(page, pre_count):
    """Wait for ChatGPT to finish responding and extract the text."""
    max_wait = TIMEOUT_MS // 1000
    start = time.time()

    # Wait for a new assistant message to appear
    while time.time() - start < 30:
        msgs = page.query_selector_all('[data-message-author-role="assistant"]')
        if len(msgs) > pre_count:
            break
        page.wait_for_timeout(500)

    # Now wait for generation to finish
    # The stop button / "thinking" indicator disappears when done
    while time.time() - start < max_wait:
        # Check if still generating
        stop_btn = page.query_selector(
            'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
        )
        thinking = page.query_selector(
            '[class*="thinking"], [class*="streaming"], [data-testid="thinking-indicator"]'
        )

        if not stop_btn and not thinking:
            # Double-check by waiting a moment and checking again
            page.wait_for_timeout(1500)
            stop_btn = page.query_selector(
                'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
            )
            if not stop_btn:
                break

        page.wait_for_timeout(1000)

    # Extract the last assistant message
    msgs = page.query_selector_all('[data-message-author-role="assistant"]')
    if not msgs:
        return "(No response received)"

    last_msg = msgs[-1]

    # Get the markdown content
    markdown_el = last_msg.query_selector(".markdown, .prose, [class*='markdown']")
    if markdown_el:
        return markdown_el.inner_text().strip()

    return last_msg.inner_text().strip()


def main():
    parser = argparse.ArgumentParser(description="Query ChatGPT via browser")
    parser.add_argument("message", nargs="?", help="Message to send")
    parser.add_argument(
        "--login", action="store_true", help="Open browser for manual login"
    )
    parser.add_argument(
        "--visible", action="store_true", help="(deprecated, always headed)"
    )
    parser.add_argument(
        "--json", "-j", action="store_true", help="Output as JSON"
    )
    args = parser.parse_args()

    if args.login:
        login_flow()
        return

    if not args.message:
        parser.error("Message required (or use --login)")

    # Always run headed — headless gets blocked by Cloudflare
    # Uses DISPLAY=:0 (NUC desktop) or DISPLAY env var
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"
    response = send_message(args.message, headless=False)

    if args.json:
        print(json.dumps({"response": response}))
    else:
        print(response)


if __name__ == "__main__":
    main()
