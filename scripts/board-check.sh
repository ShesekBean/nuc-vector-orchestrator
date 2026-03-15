#!/bin/bash
# shellcheck disable=SC2016
# board-check.sh — Review "Ophir ↔ Orchestrator" project board
#
# Lists items by status column, shows recent comments on actionable items.
# Run at start of each interactive session or on demand.
#
# Usage: bash scripts/board-check.sh

set -euo pipefail

PROJECT_NUMBER=1
OWNER="ophir-sw"
PROJECT_ID="PVT_kwHOBckgic4BQy5M"
STATUS_FIELD_ID="PVTSSF_lAHOBckgic4BQy5Mzg-z-B4"
INBOX_OPTION_ID="c0ffb956"

# ── Fetch all project items with status ──────────────────────────────────────
# shellcheck disable=SC2016  # GraphQL variables use $, not shell expansion
items_json=$(gh api graphql -f query='
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 50) {
        nodes {
          id
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
            }
          }
          content {
            ... on Issue {
              number
              title
              url
              repository { nameWithOwner }
              comments(last: 3) {
                nodes {
                  author { login }
                  body
                  createdAt
                }
              }
            }
            ... on DraftIssue {
              title
              body
            }
          }
        }
      }
    }
  }
}' -f owner="$OWNER" -F number="$PROJECT_NUMBER" 2>&1)

if echo "$items_json" | grep -q '"errors"'; then
    echo "ERROR: Failed to fetch project items"
    echo "$items_json" | jq -r '.errors[].message' 2>/dev/null
    exit 1
fi

items=$(echo "$items_json" | jq '.data.user.projectV2.items.nodes')
total=$(echo "$items" | jq 'length')

if [[ "$total" == "0" ]]; then
    echo "Board is empty — no items."
    exit 0
fi

# ── Auto-move: "Needs Input" → "Inbox" when Ophir replied ────────────────────
needs_input_replied=$(echo "$items" | jq -r '[.[] |
    select(.fieldValueByName.name == "Needs Input") |
    select(.content.comments.nodes | length > 0) |
    select(.content.comments.nodes[-1].author.login == "ophir-sw") |
    .id] | .[]' 2>/dev/null || true)

for item_id in $needs_input_replied; do
    [[ -z "$item_id" ]] && continue
    # shellcheck disable=SC2016  # GraphQL variables use $, not shell expansion
    gh api graphql -f query='
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) { projectV2Item { id } }
    }' -f projectId="$PROJECT_ID" -f itemId="$item_id" \
       -f fieldId="$STATUS_FIELD_ID" -f optionId="$INBOX_OPTION_ID" >/dev/null 2>&1
    echo "  → Moved item to Inbox (Ophir replied)"
done

# Re-fetch if we moved anything (so print output is accurate)
if [[ -n "$needs_input_replied" ]]; then
    # shellcheck disable=SC2016  # GraphQL variables use $, not shell expansion
    items_json=$(gh api graphql -f query='
    query($owner: String!, $number: Int!) {
      user(login: $owner) {
        projectV2(number: $number) {
          items(first: 50) {
            nodes {
              id
              fieldValueByName(name: "Status") {
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
              content {
                ... on Issue {
                  number
                  title
                  url
                  repository { nameWithOwner }
                  comments(last: 3) {
                    nodes { author { login } body createdAt }
                  }
                }
                ... on DraftIssue { title body }
              }
            }
          }
        }
      }
    }' -f owner="$OWNER" -F number="$PROJECT_NUMBER" 2>&1)
    items=$(echo "$items_json" | jq '.data.user.projectV2.items.nodes')
fi

# ── Print items grouped by status ────────────────────────────────────────────
print_section() {
    local status="$1"
    local emoji="$2"
    local section_items
    section_items=$(echo "$items" | jq -r --arg s "$status" '[.[] | select(.fieldValueByName.name == $s)]')
    local count
    count=$(echo "$section_items" | jq 'length')

    if [[ "$count" == "0" ]]; then
        return
    fi

    echo ""
    echo "$emoji $status ($count)"
    printf '─%.0s' {1..50}; echo

    echo "$section_items" | jq -r '.[] |
        if .content.number then
            "  #\(.content.number) \(.content.title) [\(.content.repository.nameWithOwner)]"
        else
            "  [draft] \(.content.title)"
        end'

    # For Inbox and Needs Input, show recent comments
    if [[ "$status" == "Inbox" || "$status" == "Needs Input" ]]; then
        echo "$section_items" | jq -r '.[] |
            select(.content.comments.nodes | length > 0) |
            "  └─ Recent comments on #\(.content.number):",
            (.content.comments.nodes[] |
                "     \(.author.login) (\(.createdAt | split("T")[0])): \(.body | split("\n")[0] | if length > 80 then .[:80] + "..." else . end)"
            )' 2>/dev/null || true
    fi
}

echo "═══ Ophir ↔ Orchestrator Board ═══"

print_section "Inbox" "📥"
print_section "Needs Input" "❓"
print_section "In Progress" "🔧"
print_section "Done" "✅"

echo ""
echo "───"
echo "Total: $total items"
