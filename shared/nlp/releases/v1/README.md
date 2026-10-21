<!-- CODE-VERIFY: Check app/nlp/release_catalog.py, app/services/nlp_dependencies.py, app/services/nlp_distribution.py, app/persistence/nlp_dependencies.py and tests/test_nlp_dependencies.py. -->

# Release metadata

`ReleaseCatalog` is canonical ASCII JSON signed with protected type `nlp-release-catalog.v1+jws`. Caller-supplied dedicated keys authenticate distribution metadata, independently of release approval and account authorization. Decoded metadata is limited to 1 MiB, 32 profile entries and 4,096 withdrawal digests. Generate its schema with `tools/generate_nlp_contracts.py`.

The explicit dependency service persists sequence, clock and cumulative withdrawal observations in encrypted authorization storage. Metadata expiration blocks new acquisition and selection; previously accepted installations remain usable offline under current authorization unless withdrawn. Restart requires explicit preparation. No origin, keys, exact approval provider or engine provider is configured by default.

`GET /api/v1/nlp/dependencies` returns cached summaries (at most 64 across catalog and installed metadata) and the session-bound account selection. It does not refresh, prepare or acquire. Explicit POST operations require same-origin and CSRF checks. `plan` discloses costs before `acquire` confirms its identifier. Verified files resume; the current interrupted file restarts. Account processing pause/enable is separate from acquisition pause/resume. Responses expose no filesystem paths or complete authority documents.

Data availability and preparation do not qualify a model or grant execution authority. Existing artifact-store durability and dedicated-private-parent requirements apply. Recovery deletes only journaled files belonging to an observed absent process lifetime; inaccessible or changed ownership remains unavailable.
