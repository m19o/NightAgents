# Plan Callback Migration

Version: 1
Agent: NightRouter

## Purpose

Create a non-mutating callback migration plan from an approved NightWatcher handoff.

## MCP tools

- `mythic.list_callbacks`
- `mythic.verify_callback_route`
- `mythic.plan_callback_migration`

## Rules

- Refresh callback state after approval.
- Return for reapproval when material state changed.
- Never call execute tools.
- Include preconditions, observation window and rollback steps.

