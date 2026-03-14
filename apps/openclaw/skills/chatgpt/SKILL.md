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

**Weekend rule:** If "tomorrow" falls on a Saturday or Sunday, use Monday instead. If "today" is Saturday or Sunday, use Monday.

Send this expanded prompt to the ChatGPT proxy:

> Daily brief for [TARGET DAY]. Use EXACTLY this format — no deviation, no extras, no paragraphs, no strategy essays, no offers to do more. Check my calendar, emails (7 days), Slack (48h), and recordings (7 days).
>
> 🗓 Schedule
> ⏰ HH:MM Meeting Name – one-line goal/purpose
> (list every meeting for the day, times in Pacific)
>
> 💬 Talking Points
> Person → key question or topic to raise (one per meeting, one line each)
>
> 🎯 Top 3 Must-Remember
> 1️⃣ thing one
> 2️⃣ thing two
> 3️⃣ thing three
>
> ✅ Prep Actions
> ✍️/❓/📊 concrete thing to do before meetings (max 3)
>
> 🏁 Success Outcome
> One sentence: what does a good [TARGET DAY] look like?
>
> HERE IS AN EXAMPLE of the EXACT output format I want. Copy this structure exactly, just fill in real data:
>
> 🗓 Schedule
> ⏰ 07:30 James 1:1 – align weekly focus
> ⏰ 08:00 Manish 1:1 – unblock ERP lane
> ⏰ 09:00 Product Lead Sync – lock roadmap lanes
> ⏰ 10:30 ERP/ICP – confirm target vertical
>
> 💬 Talking Points
> James → Q4 goal + blockers?
> Manish → ERP connector priority?
> Prod Lead → who owns each lane?
> ERP/ICP → manuf. vs horizontal decision?
>
> 🎯 Top 3 Must-Remember
> 1️⃣ Roadmap lanes + owners frozen
> 2️⃣ ERP-ICP path clarified
> 3️⃣ AI = seller productivity (not tech demo)
>
> ✅ Prep Actions
> ✍️ Write 3-line product thesis
> ❓ List 3 roadmap questions
> 📊 Pick seller-experience metric
>
> 🏁 Success Outcome
> Team leaves kickoff with clear lanes, owners, 12-mo milestones.
>
> END OF EXAMPLE. Now produce the real brief for [TARGET DAY] using my actual calendar, emails, Slack, and recordings. Same format, same line lengths, same emoji usage. No deviations. No extra sections. No offers.

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
