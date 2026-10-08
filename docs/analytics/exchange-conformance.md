# Synthetic exchange conformance

<!-- CODE-VERIFY: tools/qualify_analytics_exchange.py; tools/analytics_exchange_cosmos.py; tools/analytics_cosmos_node/templates.cjs -->

The conformance tool exercises `local → exchange → Cosmos Gremlin → exchange → local` using the named synthetic question fixtures. It cannot accept an application database or an arbitrary exchange file. Local tests establish local behavior. Only a successful configured run against the external target establishes compatibility for that source and fixture set.

Run the local path with:

```sh
python -m tools.qualify_analytics_exchange --target local
```

The external adapter uses Node 22 and the pinned `gremlin` 3.4.13 driver in the isolated `tools/analytics_cosmos_node` package. Install that package with `npm ci`. The application runtime and projection factory do not depend on this driver.

Supply `ANALYTICS_GREMLIN_ENDPOINT`, `ANALYTICS_GREMLIN_KEY`, `ANALYTICS_GREMLIN_DATABASE`, and `ANALYTICS_GREMLIN_GRAPH` through the process environment at launch, then run:

```sh
python -m tools.qualify_analytics_exchange --target cosmos
```

The endpoint uses TLS WebSockets. Connection values pass only through the child environment, never through exchange files, command arguments, or the JSONL protocol. Missing configuration reports `not_configured` and exits successfully. Once configured, authentication, transport, timeout, validation, and query errors fail the run. A missing configuration result is not a compatibility pass.

## Graph representation

The test graph uses `/account_ref` as its partition key. Every traversal binds the account, run namespace, and immutable generation. Physical IDs derive from the namespace, generation, and logical identity. Active graph nodes become vertices and active edges become edges between those vertices. Projection records, conversation facts, observation facts, and the envelope header have separate labels in the same partition.

Each record has a canonical JSON string payload. This preserves nulls, absence, wide integers, and active model structure despite the target's scalar property constraints. Bounded query fields are separately indexed scalar properties and are checked against the payload during full export verification. The native edge endpoints and all transport metadata are also included in that verification.

Publication writes immutable records, reads the complete set back, verifies its record digest and reconstructed exchange, checks source freshness, and finally writes one immutable manifest. The manifest binds complete membership, content, source identity, and retention. A reader requires that marker. Partial records without it do not become a published generation. Retries compare existing payloads with the expected values instead of overwriting them.

## Questions and limits

The reviewed templates select bounded observation pages for the two declared question plans. No-later-reply selection includes observations through the cutoff, including replies outside the selected date range. Pricing selection uses the selected date range and declared classification fixtures. Both paths use the same deterministic handlers as the local target for final ordering, coverage, uncertainty, and evidence selection. Callers supply bindings, never query text.

Publication pins a verified immutable bundle before a question begins. The question path checks the current source and publication manifest, reads bounded fact pages, and compares each entire fact with that pinned bundle. It does not export or scan the graph while answering a question. Every remote request in that path receives the remaining question deadline. The configured conformance allowance is 30 seconds and does not qualify the product's query latency target.

The JSONL subprocess protocol limits each request and response to 4 MiB. Record payloads are limited to 2 MiB and pages to 256 records. Driver calls have a bounded deadline. A throttled request retries at most three times and respects the target's `x-ms-retry-after-ms` TimeSpan value within that deadline. Error text is reduced to fixed codes. The result retains request charges and retry counts. Cleanup is restricted to the run's account, namespace, and generation.

The output binds results to source revision, input digests, driver lock, fixtures, and question definitions. It reports dirty source explicitly. Preserve that output outside the product repository when recording an external run. Unit tests with transport doubles do not replace this run.

The target's [supported features](https://learn.microsoft.com/en-us/azure/cosmos-db/gremlin/support) require GraphSON v2 and do not provide transactions. Its [response headers](https://learn.microsoft.com/en-us/azure/cosmos-db/gremlin/headers) define throttling delays and request charges. These constraints inform the immutable publication marker and bounded retry policy.
