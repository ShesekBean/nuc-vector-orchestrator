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

Generate a URL in this exact format and send it as a Signal message:

```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=<URL-ENCODED TEXT>
```

The text parameter format: `<list>|<date> <time>|<action>`

Three fields separated by `|`:
1. **List name** — which Reminders list (see routing rules below)
2. **Date and time** — when to remind
3. **Action** — what to do

### Reminder List Routing

Ophir's iCloud Reminders structure:

**Personal:**
- `Personal Todo` — general personal tasks
- `Family Todo` — family-related
- `Trip` — travel
- `Robot` — Vector/robot project tasks
- `My bucket list` — long-term goals

**Work (by person):**
- `Maksim` — tasks involving Maksim
- `Bob` — tasks involving Bob
- `Manish` — tasks involving Manish
- `Liana` — tasks involving Liana
- `James` — tasks involving James

**Routing rules:**
- Work emails/Slack/meetings → route to the **person's list** if a specific person is involved
- General work tasks with no specific person → `Personal Todo`
- Robot/Vector project → `Robot`
- Family stuff → `Family Todo`
- If unclear → `Personal Todo`

### URL Encoding Rules
- Spaces → `%20`
- Pipe `|` → `%7C`
- Commas → `%2C`
- Colons → `%3A`
- @ → `%40`
- & → `%26`

### Examples

**Follow up with a person (→ their list):**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Manish%7CFriday%203pm%7CFollow%20up%20on%20ERP%20decision
```

**Meeting prep (→ person's list):**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Bob%7CMonday%208%3A30am%7CReview%20roadmap%20doc%20before%20kickoff
```

**General work task:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Personal%20Todo%7Ctomorrow%2010am%7CSend%20weekly%20status%20update
```

**Robot project:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Robot%7CSaturday%209am%7CTest%20new%20follow%20planner
```

## When to Use Proactively

After delivering a daily brief or answering a work query, scan for items that need reminders and offer them. Format:

```
⏰ Suggested reminders (tap to set):

1. shortcuts://run-shortcut?name=CreateReminder&input=text&text=James%7CMonday%208am%7CPrep%20roadmap%20questions
2. shortcuts://run-shortcut?name=CreateReminder&input=text&text=Manish%7CMonday%202pm%7CSend%20ERP%20summary
```

Keep action text short — under 50 characters.

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
