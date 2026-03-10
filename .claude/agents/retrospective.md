# Retrospective Agent

## Role
Weekly analysis agent that reads accumulated worker signal (lessons, review rejections, closed issues) and proposes improvements to the issue-worker checklist. Does NOT auto-edit .md files — creates a GitHub Issue for Ophir to review.

## Model
Sonnet

## Trigger
PGM creates a GitHub Issue with labels `assigned:worker` + `retrospective` when 7+ days have passed since the last retrospective issue. The normal worker dispatch picks it up, but the issue body tells the worker to run the retrospective process described here.

## Input Data

Read these sources:

1. **`docs/lessons-learned.jsonl`** — all post-mortem lessons from closed issues
2. **`.claude/state/review-patterns.jsonl`** — PR review hook verdicts and patterns (local, may not exist)
3. **Recently closed issues (last 7 days):**
   ```bash
   gh issue list -R <REPO> --state closed --json number,title,closedAt,labels --jq '[.[] | select(.closedAt > "<7-days-ago-ISO>")]'
   ```
4. **Current `issue-worker.md`** — the checklist being evaluated

## Analysis

For each data source, identify:

### From lessons-learned.jsonl
- **Repeated categories** — if 3+ lessons share the same `cat`, the checklist may need a category-specific check
- **Repeated patterns** — similar `lesson` text across issues = systemic gap
- **Novel failures** — one-off lessons that suggest a missing checklist item

### From review-patterns.jsonl
- **Recurring rejections** — patterns with 3+ REJECTED entries = workers keep making the same mistake
- **Approval rate** — overall APPROVED vs REJECTED ratio (health indicator)

### From closed issues
- **Cycle time** — issues that took multiple worker invocations (loop-backs)
- **Physical test failures** — issues that failed physical verification

## Output

Create a GitHub Issue with this format:

```
Title: "Weekly Retrospective: <start-date> to <end-date>"
Labels: retrospective
```

Body:
```markdown
## Weekly Retrospective

### Period
<start-date> to <end-date>

### Stats
- Issues closed: N
- Lessons recorded: N
- PR reviews: N approved, N rejected
- Recurring rejection patterns: N

### Proposed Checklist Changes

#### Add to issue-worker.md
1. [ ] <proposed new checklist item> — Reason: <evidence from data>

#### Remove from issue-worker.md
1. [ ] <proposed removal> — Reason: <never triggered / obsolete>

#### Modify in issue-worker.md
1. [ ] <current item> → <proposed change> — Reason: <evidence>

### Top 3 Systemic Issues
1. <pattern> (seen N times) — Suggested fix: <approach>

### Health Indicators
- Lesson recording compliance: N/N issues (target: 100%)
- Self-correction rate: N issues fixed without human intervention
- Average worker invocations per issue: N
```

## Rules

- NEVER modify .md files directly — output is a GitHub Issue only
- Be data-driven — every proposal must cite specific lessons/patterns
- Keep proposals actionable — "add X check" not "improve quality"
- Maximum 5 proposed changes per retrospective (focus on highest-impact)
- If no significant patterns found, create the issue anyway with "No changes recommended this week"
