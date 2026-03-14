---
name: chatgpt
description: "ACTIVATE when message mentions email, emails, inbox, outlook, slack, jira, tickets, calendar, meetings, sharepoint, 'ask work', or 'work:'. Proxies the user's question to their ChatGPT web session which has access to Outlook email, Outlook calendar, Slack, and SharePoint."
metadata: {"openclaw": {"emoji": "🧠"}}
---

# ChatGPT Proxy Skill

## Overview

Ophir's ChatGPT session is connected to his business tools: **Outlook email, Outlook calendar, Slack, and SharePoint**. This skill proxies questions to ChatGPT and returns the response.

**IMPORTANT: Any question about email, calendar, Slack messages, SharePoint files, meetings, or work-related queries MUST go through this skill. Do NOT tell the user to open a browser or check manually — always query ChatGPT on their behalf.**

## How It Works

A ChatGPT API server runs on the NUC host. Query it via curl:

```bash
curl -sf -X POST http://172.17.0.1:18792/query \
  -H 'Content-Type: application/json' \
  -d '{"message": "<the user question>"}'
```

The response is JSON: `{"response": "ChatGPT's answer here"}`

If the server returns an error, tell the user the ChatGPT proxy is temporarily unavailable.

## Usage

Pass the user's full question to the API. Strip trigger prefixes like "ask work" or "work:" if present, but keep the rest of the question intact.

Examples:
- User: "check my emails" → `{"message": "check my emails"}`
- User: "ask work what jira tickets are assigned to me" → `{"message": "what jira tickets are assigned to me"}`
- User: "any new slack messages?" → `{"message": "any new slack messages?"}`
- User: "what meetings do I have today?" → `{"message": "what meetings do I have today?"}`

## Response Style

- Return ChatGPT's response as-is — don't summarize or editorialize
- If the response is very long (>2000 chars), summarize the key points
- If the API returns an error, say: "ChatGPT proxy isn't responding — I'll let Ophir know."

## Trigger Words

Activate this skill when the message contains ANY of:
- "email", "emails", "inbox", "outlook", "mail"
- "slack", "slack messages"
- "jira", "tickets", "issues"
- "calendar", "meetings", "schedule"
- "sharepoint", "files at work"
- "ask work", "work:"

## Safety

- This is a pass-through — don't inject extra instructions into the query
- If ChatGPT returns sensitive data (passwords, tokens), redact before relaying
