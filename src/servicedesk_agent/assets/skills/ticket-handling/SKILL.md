---
name: ticket-handling
description: Investigate or update an IT service desk ticket with verified tool outcomes and user constraints.
---

# Ticket Handling

1. Identify the requested ticket and the user's exact intended action.
2. Read the relevant current business record through the available ticket lookup tool.
3. Consult the operating policy when the task needs a policy decision. `search_policy`
   returns current versions; `read_policy` returns the full cited text.
4. Preserve explicit constraints, including investigation-only and keep-open requests.
5. Apply only the requested action. For destructive actions, follow the supplied
   permission and dependency protocol in its specified order.
6. Confirm the actual tool outcome. Treat business rejection and execution failure
   as separate outcomes.
7. When a handoff tool is available, save unresolved work and reference verified receipts.
