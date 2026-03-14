---
name: chatgpt
description: "ACTIVATE when message mentions email, emails, inbox, outlook, slack, jira, tickets, calendar, meetings, sharepoint, 'daily brief', 'daily prep', 'work brief', 'ask work', or 'work:'. Proxies the user's question to their ChatGPT web session which has access to Outlook email, Outlook calendar, Slack, and SharePoint."
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

## Daily Brief Shortcut

When the user says "daily brief", "daily prep", "work brief", or "brief for tomorrow", expand it into a comprehensive prompt before sending to ChatGPT. The user may specify a day (e.g. "daily brief for tomorrow", "brief for Monday"). Default to today if not specified.

Send this expanded prompt to the ChatGPT proxy:

> Give me my daily work prep briefing for [TARGET DAY]. Cover ALL of the following:
>
> 1. **CALENDAR**: List every meeting/event for [TARGET DAY]. For each: time, title, attendees, and any prep needed. Flag conflicts or back-to-backs. Also preview the following day.
>
> 2. **EMAIL TRIAGE (last 7 days)**: Scan my inbox from the past week. Group into:
>    - 🔴 ACTION REQUIRED: emails I need to respond to or act on (sender, subject, what's needed)
>    - 🟡 FYI/AWARENESS: important updates I should know but don't need to act on
>    - Skip automated notifications, digests, and spam
>
> 3. **SLACK HIGHLIGHTS (last 48 hours)**: Surface DMs or mentions I haven't responded to, important channel threads, and any decisions or announcements that affect me.
>
> 4. **RECORDINGS & MEETING NOTES (last 7 days)**: Summarize key decisions, action items assigned to me, and follow-ups from recent meetings.
>
> 5. **TOP 3 PRIORITIES**: Based on everything above, what are the 3 most important things I should focus on [TARGET DAY]? Be specific.
>
> Format for quick phone reading — headers, bullets, minimal emoji. Actionable, no fluff.

## Trigger Words

Activate this skill when the message contains ANY of:
- "email", "emails", "inbox", "outlook", "mail"
- "slack", "slack messages"
- "jira", "tickets", "issues"
- "calendar", "meetings", "schedule"
- "sharepoint", "files at work"
- "daily brief", "daily prep", "work brief"
- "ask work", "work:"

## Safety

- This is a pass-through — don't inject extra instructions into the query
- If ChatGPT returns sensitive data (passwords, tokens), redact before relaying
