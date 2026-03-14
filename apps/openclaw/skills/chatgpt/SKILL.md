---
name: chatgpt
description: "ACTIVATE when message contains 'ask work' or 'work:'. Proxies the user's question to their ChatGPT web session (which has access to Jira, Slack, email, and other connected tools) and returns the response."
metadata: {"openclaw": {"emoji": "🧠"}}
---

# ChatGPT Proxy Skill

## Overview

Ophir has ChatGPT connected to his business tools (Jira, Slack, email). This skill proxies questions to his ChatGPT web session and returns the response — so he can query those tools via Signal without opening a browser.

## How It Works

Uses Playwright to drive a real Chromium browser with Ophir's saved ChatGPT session. This passes all Cloudflare/bot checks and supports ChatGPT's connected tools (Jira, Slack, email).

Run the query script on the NUC:

```bash
python3 /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/chatgpt-query.py "<user's question with chatgpt/gpt prefix stripped>"
```

## Usage

Strip the trigger prefix ("ask work", "work:") from the user's message before passing it to the script.

Examples:
- User says: `chatgpt: what are my open jira tickets?`
- You run: `python3 .../chatgpt-query.py "what are my open jira tickets?"`
- Return the output to the user

## Follow-ups

If the user sends another chatgpt message within the same conversation, use the `-c` flag with the saved conversation ID to continue the thread. This lets ChatGPT maintain context.

Keywords that indicate a follow-up: "gpt follow up", "gpt also", "gpt and what about", or any gpt-prefixed message that clearly references a prior answer.

## Session

The script uses a persistent Chromium browser profile at `~/.openclaw/workspace/chatgpt-browser-profile/`.

If the script outputs an error about not being logged in, tell the user:
> "Your ChatGPT session has expired. Run on the NUC (with a display):
> `DISPLAY=:0 python3 scripts/chatgpt-query.py --login`
> Then log in and close the browser."

## Response Style

- Return ChatGPT's response as-is — don't summarize or editorialize
- If ChatGPT used tools (Jira, email, etc.), mention which tools were accessed
- If the response is very long (>2000 chars), summarize the key points and mention full output is available

## Trigger Words

- "ask work" (prefix)
- "work:" (prefix)

Messages without these triggers should NOT activate this skill.

## Safety

- Never modify the auth token file
- Never send the auth token in messages
- This is a pass-through — don't inject extra instructions into the ChatGPT query
- If ChatGPT returns sensitive data (passwords, tokens), redact before relaying
