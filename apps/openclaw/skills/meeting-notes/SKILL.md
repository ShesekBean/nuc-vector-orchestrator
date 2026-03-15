---
name: meeting-notes
description: "ACTIVATE when user sends an audio file, voice note, or says 'meeting notes', 'summarize meeting', 'meeting summary', or 'action items'. Transcribes meeting recordings, identifies the meeting from calendar, summarizes key points, and sends action items as tappable reminder links."
metadata: {"openclaw": {"emoji": "📝"}}
---

# Meeting Notes Skill

## Overview

Ophir records meetings on his phone and sends the audio via Signal. This skill transcribes the recording, identifies which meeting it was (from calendar), extracts key decisions and action items, and sends tappable reminder links for follow-ups.

## Flow

1. **Receive audio** — user sends audio file (.m4a, .aac, .ogg, .wav, .mp3) on Signal
2. **Identify meeting** — check today's calendar via ChatGPT proxy to match the recording to a meeting
3. **Transcribe** — use the audio file content (OpenClaw can read audio attachments)
4. **Summarize** — extract key points, decisions, action items
5. **Send reminders** — generate tappable shortcuts:// links for each action item

## When Audio Is Received

When the user sends an audio file on Signal:

1. Read the audio file from the inbound media path
2. Ask the ChatGPT proxy what meetings were on the calendar today/recently:
   ```bash
   curl -sf -X POST http://172.17.0.1:18792/query \
     -H 'Content-Type: application/json' \
     -d '{"message": "what meetings did I have today? just list time and title"}'
   ```
   Poll for result, then ask the user which meeting this recording is from if unclear.

3. Transcribe and summarize the audio content

4. Produce output in this EXACT format:

```
📝 Meeting Notes: [Meeting Title]
📅 [Date] [Time] PT

## Key Decisions
- [decision 1]
- [decision 2]

## Action Items
[For each action item, include a tappable reminder link]

⏰ [Person] — [action] ([due date])
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22[URL-encoded action]%22%2C%22list%22%3A%22[Person]%22%2C%22when%22%3A%22[Month Day, Year at HH:MM AM/PM]%22%7D

## Summary
[3-5 sentence summary of the meeting]
```

## Reminder Link Format

Same as the reminders skill — JSON with title, list, when:
```json
{"title":"Follow up on X","list":"Maksim","when":"March 17, 2026 at 9:00 AM"}
```

URL-encoded in: `shortcuts://run-shortcut?name=CreateReminder&input=text&text=<encoded JSON>`

### Person → List Routing
- Maksim, Bob, Manish, Liana, James → their named list
- General/self tasks → `Personal Todo`
- Robot project → `Robot`

### Due Date Rules
- If mentioned in meeting: use that date
- "By end of week" → Friday 5:00 PM
- "Next week" → Monday 9:00 AM
- No date mentioned → next business day 9:00 AM
- All times in Pacific

## When User Says "meeting notes" or "summarize meeting"

If no audio attached, ask: "Send me the recording and I'll summarize it."

If audio was recently sent (last message was audio), process that file.

## When User Says "action items from [meeting name]"

Look up recent meeting notes in memory and re-send the action item reminder links.

## Trigger Words

- Audio file attachment (any audio MIME type)
- "meeting notes", "meeting summary", "summarize meeting"
- "action items", "what were the action items"
- "summarize this recording", "transcribe this"

## Response Style

- Lead with the meeting title and time
- Key decisions as bullets (max 5)
- Action items with tappable reminder links (max 8)
- Summary last (3-5 sentences)
- All times in Pacific
- Keep it scannable — this is read on a phone
