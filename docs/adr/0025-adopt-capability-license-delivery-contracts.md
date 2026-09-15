# ADR 0025: Adopt CapabilityLicense delivery contracts

- Status: Accepted
- Date: 2026-09-13

## Decision

The product adopts the published cross-plane CapabilityLicense delivery architecture from `ramiradwan/creator-platform-contracts` at commit `50c08ee8b3f3dbb1364b875e876a32ab7c641f9a` (tree `15b821c361f4bc1077a0e1ef5689f5916ff75f51`) as the contract authority for subsequent implementation.

New production enrollment uses `urn:bridge-clean:installation-claim:v2`. Matching ambiguous-response recovery uses `urn:bridge-clean:bootstrap-recovery:v2`. Enrollment returns installation and membership bootstrap authority only; it does not return commercial runtime authority.

CapabilityLicense commercial authority is delivered separately, after an explicit commercial exchange. For new production runtime, `urn:bridge-clean:capability-license:v1` replaces legacy `license_entitlement` as commercial authority. This adoption does not redefine the signed CapabilityLicense contract.

CapabilityLicense never supplies principal, account, or creator identity. Identity/account authority and commercial authority remain separately verified inputs to RuntimePolicy. The licensed capability is `analysis-run`. Existing data access, export, delete, and rebuild access does not depend on CapabilityLicense.

After activation, ordinary licensed execution remains local and offline. This decision introduces no hosted runtime heartbeat, execution reservation, execution counter, result reporting, or execution telemetry.

## Why

Enrollment establishes an installation and membership bootstrap boundary; it is not evidence of a customer's commercial decision. Keeping bootstrap identity authority separate from CapabilityLicense authority prevents either plane from silently becoming a substitute for the other and preserves the product's local-first runtime boundary.

The published delivery contracts also define deterministic installation-claim v2 handoff, recovery, explicit quote/exchange, activation proof, activation package, and replacement-reissue flows without changing signed CapabilityLicense v1 semantics. Adopting those contracts first creates a provenance-checked foundation for later runtime work without changing current runtime behavior in this checkpoint.

## Consequences

The vendored contract snapshot now pins the exact published contracts repository commit and tree, includes the promoted schemas/profiles/OpenAPI/catalog plus delivery and compatibility vectors, and mechanically verifies those bytes against the published source manifest. Released v1 installation-claim material, permit material, and existing production trust material remain present for compatibility.

CapabilityLicense fixture signing trust remains conformance-test material only. This ADR does not designate fixture CapabilityLicense keys as production trust and does not select the production CapabilityLicense trust distribution mechanism; that must be resolved before runtime verification is enabled.

This checkpoint does not change Brain enrollment behavior, hosted requests, CapabilityLicense verification or persistence, RuntimePolicy enforcement, Agent pairing, extension behavior, or packaging.

## Related

- ADR 0008: Separate hosted provisioning from local runtime authentication
- ADR 0009: Use a local-first runtime and persistence boundary
- ADR 0014: Define the local authorization foundation
- ADR 0015: Define the non-expiring capability licence profile
- Published contracts ADR 0020: Adopt installation-claim v2 and CapabilityLicense delivery
