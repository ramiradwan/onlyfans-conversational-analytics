# Offline contract snapshot

This is a selected export set, not a complete copy of its source. It contains
grant-profile and capability-permit fixtures, permit-consumption policy vectors,
the selected ADR 0012 onboarding-progress cases, production grant trust material,
and the minimal closed schema dependency set for those contracts. The progress
schemas and vectors are pinned to approved source commit
`ce510faf767e8d808a04eb9ceb28523b598eac0f`; the installation-claim export is not
selected. `manifest.json` records the selected bytes and their aggregate digest;
`consumer-pin.json` independently pins that manifest, the vector manifests, trust
sets, generator versions, supported profiles, and selected export set.
`python -m contracts.verify` verifies both records before a trust set can be
loaded.

Fixture trust sets are test-only and cannot authorize production work: loading
one outside `development` is refused.

The `production/` tree contains the manifest-covered grant verification trust set.

## Approved updates

Regenerate only from a clean, approved source checkout through the one
controlled script:

```powershell
python tools/regenerate_contract_snapshot.py --copy-from <approved-source-checkout>
python tools/regenerate_contract_snapshot.py --check
python -m contracts.verify
```

Never hand-edit a fixture, manifest, or hash. Released `v1` bytes are
append-only: a semantic change requires a new profile-version directory, never
an edit to this one.
