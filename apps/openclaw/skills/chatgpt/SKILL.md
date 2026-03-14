---
name: chatgpt
description: "ACTIVATE when message mentions email, emails, inbox, outlook, slack, jira, tickets, calendar, meetings, sharepoint, 'ask work', or 'work:'. Proxies the user's question to their ChatGPT web session which has access to Outlook email, Outlook calendar, Slack, and SharePoint."
metadata: {"openclaw": {"emoji": "🧠"}}
---

# ChatGPT Proxy Skill

## Overview

Ophir's ChatGPT session is connected to his business tools: **Outlook email, Outlook calendar, Slack, and SharePoint**. This skill proxies questions to ChatGPT and returns the response.

**IMPORTANT: Any question about email, calendar, Slack messages, SharePoint files, meetings, or work-related queries MUST go through this skill. Do NOT tell the user to open a browser or check manually — always query ChatGPT on their behalf.**

## How It Works — Async (2 steps)

The ChatGPT proxy uses an async pattern because queries take 10-60 seconds. You MUST follow both steps:

### Step 1: Submit the query (instant response)
```bash
curl -sf -X POST http://172.17.0.1:18792/query \
  -H 'Content-Type: application/json' \
  -d '{"message": "<the user question>"}'
```
Response: `{"job_id": "abc123def456"}`

### Step 2: Poll for result (repeat every 5 seconds until done)
```bash
curl -sf http://172.17.0.1:18792/result/<job_id>
```
- If pending: `{"status": "pending", "message": "Still working..."}`  → wait 5 seconds and poll again
- If done: `{"status": "done", "response": "ChatGPT's answer here"}`
- If error: `{"status": "error", "response": "error details"}`

### Example complete flow:
```bash
# Step 1: Submit
JOB=$(curl -sf -X POST http://172.17.0.1:18792/query -H 'Content-Type: application/json' -d '{"message": "check my emails"}')
JOB_ID=$(echo "$JOB" | jq -r '.job_id')

# Step 2: Poll (repeat until status != pending)
sleep 5
curl -sf http://172.17.0.1:18792/result/$JOB_ID
# If still pending, sleep 5 and curl again
```

**CRITICAL: You MUST poll multiple times. The first poll will usually return "pending". Keep polling every 5 seconds until you get "done" or "error". Expect 3-10 polls for email/tool queries.**

## Usage

Pass the user's full question to the API. Strip trigger prefixes like "ask work" or "work:" if present.

## Response Style

- Return ChatGPT's response as-is — don't summarize or editorialize
- If the response is very long (>2000 chars), summarize the key points
- If the API returns an error, say: "ChatGPT proxy had an issue, let me try again." and retry once.

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
