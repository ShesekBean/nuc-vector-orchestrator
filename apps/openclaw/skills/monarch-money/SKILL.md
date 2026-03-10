---
name: monarch-money
description: "ACTIVATE when message mentions money, finances, spending, budget, accounts, balance, net worth, transactions, or 'monarch'. Queries Monarch Money for account balances, spending breakdowns, budgets, transactions, and net worth trends."
metadata: {"openclaw": {"emoji": "💰"}}
---

# Monarch Money Skill

## Overview

You have READ-ONLY access to Ophir's financial data via the Monarch Money GraphQL API. When the user asks about money, spending, balances, budgets, or transactions, query the API and report the results clearly.

**SECURITY: This skill is READ-ONLY. Never construct mutation queries. Never modify financial data.**

## Persistent Log

File: `memory/monarch-log.json` (relative to workspace root). Read it first on every task.

Schema:
```json
{
  "session_token": "...",
  "session_created_at": "YYYY-MM-DD",
  "last_query_at": "YYYY-MM-DD"
}
```

## Authentication

All API calls require the session token from `memory/monarch-log.json`.

If the token is missing or a query returns 401/403, tell the user:
> "Your Monarch Money session has expired. Run `python3 scripts/monarch-login.py` on the NUC to refresh it."

Do NOT attempt to log in or handle credentials yourself.

## API Reference

- **Base URL:** `https://api.monarchmoney.com`
- **GraphQL endpoint:** `https://api.monarchmoney.com/graphql`

### Required Headers

Every request must include:
```
Content-Type: application/json
Authorization: Token <session_token from monarch-log.json>
Accept: application/json
Client-Platform: web
```

### How to Call

All queries use POST to the GraphQL endpoint. Example:
```bash
curl -sf -X POST https://api.monarchmoney.com/graphql \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Token <TOKEN>' \
  -H 'Accept: application/json' \
  -H 'Client-Platform: web' \
  -d '{"operationName":"GetAccounts","query":"query GetAccounts { accounts { id displayName currentBalance isAsset isHidden includeInNetWorth type { name display } subtype { name display } institution { name } } }","variables":{}}'
```

## Queries

### 1. Account Balances — "monarch balances" / "how much money do I have"

```graphql
query GetAccounts {
  accounts {
    id
    displayName
    currentBalance
    displayBalance
    isAsset
    isHidden
    includeInNetWorth
    syncDisabled
    deactivatedAt
    type { name display }
    subtype { name display }
    institution { name }
  }
}
```
Variables: `{}`

**How to present:**
- Filter out hidden accounts (`isHidden: true`) and deactivated accounts (`deactivatedAt != null`)
- Group by type (checking, savings, credit card, investment, loan, etc.)
- Show each account: name, institution, balance
- Show totals per type
- Show net worth total (sum of all accounts where `includeInNetWorth: true`, with credit/loan as negative)
- Format currency with commas and 2 decimal places

### 2. Net Worth Trend — "monarch net worth" / "net worth trend"

```graphql
query GetAggregateSnapshots($filters: AggregateSnapshotFilters) {
  aggregateSnapshots(filters: $filters) {
    date
    balance
  }
}
```
Variables:
```json
{
  "filters": {
    "startDate": "<30 days ago YYYY-MM-DD>",
    "endDate": "<today YYYY-MM-DD>"
  }
}
```

**How to present:**
- Show current net worth
- Show 30-day change (latest balance minus earliest balance)
- Note the trend direction

### 3. Spending / Cashflow — "monarch spending" / "where did my money go"

```graphql
query Web_GetCashFlowPage($filters: TransactionFilterInput) {
  byCategory: aggregates(filters: $filters, groupBy: ["category"]) {
    groupBy {
      category {
        id
        name
        group { id type name }
      }
    }
    summary { sum }
  }
  byCategoryGroup: aggregates(filters: $filters, groupBy: ["categoryGroup"]) {
    groupBy {
      categoryGroup { id name type }
    }
    summary { sum }
  }
  byMerchant: aggregates(filters: $filters, groupBy: ["merchant"]) {
    groupBy {
      merchant { id name }
    }
    summary { sumIncome sumExpense }
  }
  summary: aggregates(filters: $filters, fillEmptyValues: true) {
    summary {
      sumIncome
      sumExpense
      savings
      savingsRate
    }
  }
}
```
Variables:
```json
{
  "filters": {
    "startDate": "<start YYYY-MM-DD>",
    "endDate": "<end YYYY-MM-DD>"
  }
}
```

**Time frame defaults:**
- "this week" → Monday of current week to today
- "this month" → 1st of current month to today
- "last month" → 1st to last day of previous month
- "this year" → Jan 1 to today
- No time frame specified → default to current month

**How to present:**
- Lead with summary: total income, total expenses, savings, savings rate
- Then top 10 expense categories sorted by amount (largest first)
- Then top 5 merchants by expense
- Format all amounts as currency

### 4. Transactions — "monarch transactions" / "what did I spend on"

```graphql
query GetTransactionsList($offset: Int, $limit: Int, $filters: TransactionFilterInput, $orderBy: TransactionOrdering) {
  allTransactions(filters: $filters) {
    totalCount
    results(offset: $offset, limit: $limit, orderBy: $orderBy) {
      id
      amount
      pending
      date
      notes
      isRecurring
      needsReview
      category { id name }
      merchant { name id }
      account { id displayName }
      tags { id name }
    }
  }
}
```
Variables:
```json
{
  "offset": 0,
  "limit": 25,
  "orderBy": "date",
  "filters": {
    "startDate": "<start YYYY-MM-DD>",
    "endDate": "<end YYYY-MM-DD>"
  }
}
```

**Filter support:**
- By category: add `"categories": ["<category_id>"]` to filters
- By account: add `"accounts": ["<account_id>"]` to filters
- By search term: add `"search": "<term>"` to filters

**How to present:**
- List transactions: date, merchant, category, amount, account
- Negative amounts = expenses, positive = income
- If user asks about a specific category or merchant, filter accordingly
- Show total count and current page

### 5. Budgets — "monarch budget" / "am I over budget"

```graphql
query Common_GetJointPlanningData($startDate: Date!, $endDate: Date!) {
  budgetData(startMonth: $startDate, endMonth: $endDate) {
    monthlyAmountsByCategory {
      category { id name }
      monthlyAmounts {
        month
        plannedCashFlowAmount
        actualAmount
        remainingAmount
        isFlexExpense
      }
    }
    totalsByMonth {
      month
      totalExpenses { plannedAmount actualAmount remainingAmount }
      totalIncome { plannedAmount actualAmount remainingAmount }
    }
  }
}
```
Variables:
```json
{
  "startDate": "<1st of current month YYYY-MM-DD>",
  "endDate": "<last day of current month YYYY-MM-DD>"
}
```

**How to present:**
- Show overall budget: planned vs actual (income and expenses)
- List categories that are over budget (actual > planned, where planned > 0)
- List categories approaching budget (>80% used)
- Show remaining budget for the month
- Use color language: "over" for exceeded, "on track" for under

### 6. Categories — helper query (use when user mentions a category by name)

```graphql
query GetCategories {
  categories {
    id
    name
    group { id name type }
  }
}
```
Variables: `{}`

Use this to resolve category names to IDs when the user asks to filter by category (e.g., "how much did I spend on groceries"). Cache the result during the conversation to avoid repeat calls.

### 7. Recurring Transactions — "monarch recurring" / "upcoming bills"

```graphql
query Web_GetUpcomingRecurringTransactionItems($startDate: Date!, $endDate: Date!) {
  recurringTransactionItems(startDate: $startDate, endDate: $endDate) {
    stream {
      id
      frequency
      isApproximate
      merchant { name }
      category { id name }
      account { id displayName }
    }
    date
    amount
    isPast
  }
}
```
Variables:
```json
{
  "startDate": "<today YYYY-MM-DD>",
  "endDate": "<30 days from now YYYY-MM-DD>"
}
```

**How to present:**
- List upcoming bills/subscriptions sorted by date
- Show: date, merchant, amount, category, frequency
- Total upcoming expenses for the period

## Trigger Words

Activate this skill when the message contains any of:
- "monarch" (explicit trigger)
- "balance", "balances", "how much money", "account balance"
- "net worth"
- "spending", "spent", "expenses", "where did my money go"
- "budget", "over budget", "under budget"
- "transactions", "transaction history", "charges"
- "bills", "subscriptions", "recurring"
- "cashflow", "cash flow", "income vs expenses"
- "finances", "financial", "money summary"

Without these triggers, do NOT query the Monarch API — just chat normally.

## Response Style

1. Lead with the headline number (total balance, net worth, spending total)
2. Then break down the details
3. Keep it scannable — use bullet points or short lines
4. Always include the time period for time-based queries
5. Round to 2 decimal places, use $ prefix, use commas for thousands
6. If something looks unusual (big spike in spending, account sync issue), mention it briefly
7. End with a one-liner insight or observation when relevant

Example for "monarch spending this month":
> **March spending so far: $3,247.82**
> Income: $5,200.00 | Savings rate: 37.6%
>
> Top categories:
> - Rent: $1,800.00
> - Groceries: $412.33
> - Dining out: $287.50
> - Subscriptions: $145.99
> - Gas: $89.00
>
> Dining out is up 40% vs last month — might be worth watching.

## Safety Rules

- **READ-ONLY**: Never construct GraphQL mutations. Only use the queries listed above.
- **No credentials**: Never ask for or store email/password. Only use the session token.
- **No external sharing**: Financial data stays between you and Ophir. Never include it in outbound messages to other contacts.
- **Token expiry**: If any query returns 401/403, stop and tell the user to re-authenticate.
- **Rate limiting**: Don't fire more than 5 queries in a single conversation turn. Batch what you can.
