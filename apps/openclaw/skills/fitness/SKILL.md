---
name: fitness
description: "Personal fitness coach. Tracks swim and runs via Strava, weight via Withings scale, readiness via Oura. Notifies a friend about weekly runs."
metadata: {"openclaw": {"emoji": "🏃"}}
---

# Fitness Skill

## Goals
- Swim every morning. Check Strava at noon.
- Run 4K once/week + 8K once/week. Track on Strava.
- Weight goal: target kg. Pull automatically from Withings scale.
- Diet: log meals at 1 PM and 8 PM. Give brief honest feedback.
- Notify run buddy about weekly run on Wednesdays (outbound Signal only).

## Persistent Log
File: `memory/fitness-log.json` (relative to workspace root). Read it first on every task.

Schema:
```json
{
  "weights": [{"date": "YYYY-MM-DD", "kg": 0.0}],
  "food": [{"date": "YYYY-MM-DD", "meal": "lunch", "text": "..."}],
  "swims": [{"date": "YYYY-MM-DD", "activity_id": 0}],
  "runs": [{"date": "YYYY-MM-DD", "distance_km": 0.0, "activity_id": 0}],
  "strava_token_cache": {"access_token": "...", "refresh_token": "...", "expires_at": 0},
  "withings_token_cache": {"access_token": "...", "refresh_token": "...", "expires_at": 0}
}
```

## Withings API (weight from scale)

Tokens are refreshed automatically every 2 hours by `fitness-token-refresh.timer` (systemd user timer on NUC). The timer runs `scripts/refresh-fitness-tokens.py` inside the openclaw-gateway container.
If `withings_token_cache.expires_at < now`, the timer will refresh it within 2 hours. Tell the user the token is being refreshed and to try again shortly. Do NOT attempt to refresh tokens yourself.

```
GET https://wbsapi.withings.net/measure?action=getmeas&meastypes=1&category=1&lastupdate=<unix 48h ago>
Authorization: Bearer <withings_token_cache.access_token from fitness-log.json>
```

Parse response: `body.measuregrps` → pick entry with highest `date` → find measure with `type == 1`.
Weight in kg = `value * 10^unit` (e.g. value=90861, unit=-3 → 90.861 kg).

Report: current weight, delta from last logged entry, progress toward goal weight.
Example: "90.9 kg this morning (-0.3 from yesterday). 4.9 kg to go."
Log result to `weights` array in fitness-log.json.

## Strava API (swim + run)

Tokens are refreshed automatically every 2 hours by `fitness-token-refresh.timer` (systemd user timer on NUC). The timer runs `scripts/refresh-fitness-tokens.py` inside the openclaw-gateway container.
If `strava_token_cache.expires_at < now`, the timer will refresh it within 2 hours. Tell the user the token is being refreshed and to try again shortly. Do NOT attempt to refresh tokens yourself.

```
GET https://www.strava.com/api/v3/athlete/activities?after=<unix midnight today>&per_page=20
Authorization: Bearer <strava_token_cache.access_token from fitness-log.json>
```

- Swim: `type == "Swim"`
- Run: `type == "Run"`, `distance` in meters (÷1000 for km)

For weekly runs use `after=<unix midnight Monday this week>`.

## Oura API (readiness + sleep)

Oura uses a Personal Access Token (does not expire, no refresh needed).
Token is in `.env` as `OURA_ACCESS_TOKEN`.

### Readiness
```
GET https://api.ouraring.com/v2/usercollection/daily_readiness?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
Authorization: Bearer $OURA_ACCESS_TOKEN
```
Use `score` as context. Score < 60 → acknowledge lower readiness, suggest recovery day.

### Sleep
```
GET https://api.ouraring.com/v2/usercollection/daily_sleep?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
Authorization: Bearer $OURA_ACCESS_TOKEN
```
Key fields: `score`, `contributors.total_sleep`, `contributors.deep_sleep`, `contributors.rem_sleep`.

### Sleep debt (custom)
When user asks "what is my sleep debt":
- Assume target sleep = **7.0h/day**
- Use a rolling **7-day window**
- Query:
```
GET https://api.ouraring.com/v2/usercollection/sleep?start_date=<10 days ago>&end_date=<today>
Authorization: Bearer $OURA_ACCESS_TOKEN
```
- For each day, use the largest `total_sleep_duration` (seconds) as main sleep
- Take the latest 7 days with data and compute:
  - `target_hours = 49`
  - `actual_hours = sum(total_sleep_duration)/3600`
  - `sleep_debt_hours = max(0, target_hours - actual_hours)`
- Respond with sleep debt in hours (1 decimal), plus actual vs target.

## Diet Guidance
- Encourage: protein-rich, Mediterranean-style, vegetables, lean protein
- Flag: excessive sugar, alcohol, ultra-processed food, heavy late-night meals
- Feedback: 1–3 lines, honest, not preachy
- End-of-day: one thing that went well, one thing to improve tomorrow

## Tone
- Direct and encouraging, no sycophancy
- Celebrate streaks and milestones (every 0.5 kg lost)
- Missing a day: acknowledge briefly, move forward
- Run buddy texts: casual, outbound only

## Wednesday Run Coordination
1. Check Strava for this week's runs (Mon–today). Report: short run done? Long run done?
2. Send outbound Signal message to run buddy suggesting a run day/time. Keep it casual.
