---
name: reminders
description: "CREATE reminders proactively when you detect action items, deadlines, follow-ups, or meeting prep needs. Also ACTIVATE when user says 'remind me', 'set a reminder', or 'reminder'. Sends a tappable shortcuts:// link on Signal that creates an iOS Reminder."
metadata: {"openclaw": {"emoji": "⏰"}}
---

# Reminders Skill

## Overview

You can create reminders on Ophir's iPhone by sending a tappable `shortcuts://` link via Signal. When tapped, it opens the iOS Shortcuts app which creates the reminder automatically.

**PROACTIVE USE: You should create reminders whenever you detect:**
- Action items from emails or Slack ("respond to X by Friday")
- Meeting prep needed ("review doc before 2pm meeting")
- Follow-ups mentioned in daily briefs
- Deadlines approaching
- Anything the user asks to be reminded about

## How to Create a Reminder

Generate a URL in this exact format and send it as a tappable Signal message:

```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=<URL-ENCODED JSON>
```

The text parameter is a JSON object with three fields:
```json
{"title":"<action>","list":"<list name>","when":"<Month Day, Year at HH:MM AM/PM>"}
```

### Date format
Always use: `March 15, 2026 at 10:00 AM` (full month name, full year, 12-hour with AM/PM)

### Reminder List Routing

Ophir's iCloud Reminders lists:

**Personal:**
- `Personal Todo` — general personal tasks
- `Family Todo` — family-related
- `Trip` — travel
- `Family ⚠️` — urgent family
- `Robot` — Vector/robot project tasks
- `My bucket list` — long-term goals

**Work (by person):**
- `Maksim` — tasks involving Maksim
- `Bob` — tasks involving Bob
- `Manish` — tasks involving Manish
- `Liana` — tasks involving Liana
- `James` — tasks involving James

**Routing rules:**
- Work task involving a specific person → that person's list
- General work tasks → `Personal Todo`
- Robot/Vector project → `Robot`
- Family → `Family Todo`
- If unclear → `Personal Todo`

### URL Encoding Rules
- Spaces → `%20`
- `{` → `%7B`, `}` → `%7D`
- `"` → `%22`
- `:` → `%3A`
- `,` → `%2C`
- `@` → `%40`

### Examples

**Follow up with a person:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22Follow%20up%20on%20ERP%20decision%22%2C%22list%22%3A%22Manish%22%2C%22when%22%3A%22March%2021%2C%202026%20at%203%3A00%20PM%22%7D
```

**Meeting prep:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22Review%20roadmap%20doc%20before%20kickoff%22%2C%22list%22%3A%22Bob%22%2C%22when%22%3A%22March%2017%2C%202026%20at%208%3A30%20AM%22%7D
```

**Robot project:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22Test%20new%20follow%20planner%22%2C%22list%22%3A%22Robot%22%2C%22when%22%3A%22March%2015%2C%202026%20at%209%3A00%20AM%22%7D
```

## When to Use Proactively

After delivering a daily brief or answering a work query, scan for items that need reminders and offer them. Format:

```
⏰ Suggested reminders (tap to set):

1. Prep roadmap questions (James, Mon 8am)
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22Prep%20roadmap%20questions%22%2C%22list%22%3A%22James%22%2C%22when%22%3A%22March%2017%2C%202026%20at%208%3A00%20AM%22%7D

2. Send ERP summary (Manish, Mon 2pm)
shortcuts://run-shortcut?name=CreateReminder&input=text&text=%7B%22title%22%3A%22Send%20ERP%20summary%22%2C%22list%22%3A%22Manish%22%2C%22when%22%3A%22March%2017%2C%202026%20at%202%3A00%20PM%22%7D
```

Always add a human-readable label before each link. Keep action text under 50 characters.

## When User Asks Directly

If the user says "remind me to X at Y", generate the link immediately. Don't ask for confirmation — just send the tappable link.

## Trigger Words

- "remind me", "reminder", "set a reminder"
- Also use PROACTIVELY after daily briefs and action-item-heavy responses

## Time Defaults

- "tomorrow" → next day
- "Monday" → next Monday
- No time specified → default to 9:00am
- "end of day" → 5:00pm
- "morning" → 8:00am
- "afternoon" → 1:00pm
