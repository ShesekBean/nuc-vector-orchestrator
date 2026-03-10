#!/usr/bin/env python3
"""Smart comment summarization for agent-loop.

Reads GitHub issue comments JSON from stdin (gh issue view --json comments).
Outputs formatted text: older comments as one-line role header summaries,
recent comments as full text. Preserves the entire issue history in compact
form so agents don't lose critical context (requirements, design decisions,
test results).
"""
import json
import sys

RECENT = 10  # number of recent comments to include in full


def summarize_comment(comment):
    """Extract one-line summary from a comment (role header + title)."""
    body = comment["body"].strip()
    first_line = body.split("\n")[0].strip()
    # Agent comments start with "## EMOJI ROLE: Title" — extract that
    if first_line.startswith("## "):
        return first_line[3:]
    # Agent session failures
    if first_line.startswith("Agent session"):
        return first_line
    # Fallback: first 150 chars
    return first_line[:150]


def main():
    data = json.load(sys.stdin)
    comments = data.get("comments", [])
    total = len(comments)

    if total == 0:
        return

    lines = []

    if total <= RECENT:
        # All comments fit — output full text
        for c in comments:
            lines.append(f'**{c["author"]["login"]}** ({c["createdAt"]}):')
            lines.append(c["body"])
            lines.append("---")
    else:
        # Split into older (summarized) and recent (full text)
        older = comments[: total - RECENT]
        recent = comments[total - RECENT :]

        lines.append(
            f"=== OLDER COMMENTS ({len(older)} summarized — "
            "use `gh issue view` to read full text) ==="
        )
        for c in older:
            summary = summarize_comment(c)
            ts = c["createdAt"][:16]  # YYYY-MM-DDTHH:MM
            author = c["author"]["login"]
            lines.append(f"- {author} ({ts}): {summary}")

        lines.append("\n=== RECENT COMMENTS (full text) ===")
        for c in recent:
            lines.append(f'**{c["author"]["login"]}** ({c["createdAt"]}):')
            lines.append(c["body"])
            lines.append("---")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
