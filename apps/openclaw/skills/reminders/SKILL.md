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

The text parameter format: `Remind me <date> <time> to <action>`

### URL Encoding Rules
- Spaces → `%20`
- Newlines → `%0A`
- Commas → `%2C`
- Colons → `%3A`
- Quotes → `%22`
- @ → `%40`
- & → `%26`

### Examples

**Simple reminder:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Remind%20me%20tomorrow%2010am%20to%20email%20John
```

**Meeting prep:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Remind%20me%20Monday%208%3A30am%20to%20review%20roadmap%20doc%20before%209am%20kickoff
```

**Follow-up:**
```
⏰ Tap to set reminder:
shortcuts://run-shortcut?name=CreateReminder&input=text&text=Remind%20me%20Friday%203pm%20to%20follow%20up%20with%20Manish%20on%20ERP%20decision
```

## When to Use Proactively

After delivering a daily brief or answering a work query, scan for items that need reminders and offer them. Format:

```
⏰ Suggested reminders (tap to set):

1. shortcuts://run-shortcut?name=CreateReminder&input=text&text=Remind%20me%20Monday%208am%20to%20prep%20roadmap%20questions
2. shortcuts://run-shortcut?name=CreateReminder&input=text&text=Remind%20me%20Monday%202pm%20to%20send%20Manish%20the%20ERP%20summary
```

Keep reminder text short and actionable — under 60 characters if possible.

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
