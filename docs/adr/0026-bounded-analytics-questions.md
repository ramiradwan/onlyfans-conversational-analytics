# ADR 0026: Use bounded, source-linked analytics questions

- Status: accepted
- Date: 2026-09-18

## Decision

Expose approved analytics questions over account-scoped local projections. Keep canonical conversation data authoritative and graph/search indexes replaceable.

Adopt the record separation, source fidelity, explicit uncertainty, and versioned metric definitions in ADR 0013 for these questions. Other proposals in ADR 0013 remain proposed. This decision does not change capture, protocol v2, persistence topology, authorization, or retention.

The [question contract](../analytics/questions.md) defines populations, time boundaries, evidence, ordering, and result states. Fixed synthetic examples define expected answers independently of production analyzers or rebuilds.

Use deterministic code for observed facts. Evaluate rules and task-specific classifiers before considering an LLM. The [local analysis policy](../analytics/local-analysis.md) makes installer size, dependency size, CPU use, and memory part of model acceptance.

Optional analysis must not block existing local access or require a GPU, cloud inference, or an external graph server. Adding downloadable executable runtimes or another process requires a separate packaging and security decision before implementation.

## Why

Creators need results they can inspect in their conversations. A reproducible question definition matters more than the database or model that executes it.

Local and Cosmos implementations must produce equivalent answers for the declared subset. This decision does not claim general Gremlin compatibility or authorize uploading customer data.

## Consequences

Store observations, classifications, and outcome measurements separately. A topic or amount mentioned in a message is not a payment. Message order is not proof of causation.

Adapters must preserve source versions, coverage, and ordering uncertainty. Missing information cannot become a negative finding. Customer text explains the result and its limits without exposing internal version identifiers.

## Related

- [Canonical analytics scope](0013-conversational-analytics-scope.md)
- [Local runtime and persistence](0009-local-first-topology-and-persistence.md)
- [Analytics publication](0020-projection-store-topology-and-activation.md)
- [Analytics qualification](../analytics/qualification.md)
