<!-- CODE-VERIFY: Verify service names, ownership boundaries, Agent configuration behavior, command behavior, and analytics fallback rules against service and persistence source before editing. -->

# Brain services

`app/services/` contains application workflows used by Brain endpoints. Services coordinate policy and work; persistence remains responsible for authoritative conversation state and atomic data changes.

## Responsibilities

- `agent_configuration.py` builds account-scoped Agent configuration and tracks required and applied revisions.
- `command_execution.py` records allowed commands, delivery attempts, and command results.
- Other services coordinate ingestion, enrichment, graph work, analytics reads, and paging without becoming a second source of canonical conversation data.

## Boundaries

- Endpoints authenticate and validate requests; services coordinate application work; persistence owns authoritative state changes.
- Agent, not Brain services, owns signer page reads, upstream cursors, history scheduling, and durable capture sequencing.
- The authenticated or bound local session supplies account authority. A client-provided account value is not authority by itself.
- Raw platform responses stay inside Agent and must not be written to Brain diagnostics.
- When derived analytics are unavailable, services report them as unavailable rather than substituting sample or static values.

See [Brain](../README.md), [Brain API endpoints](../api/endpoints/README.md), and [Analytics](../analytics/README.md) for the surrounding boundaries.
