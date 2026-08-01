# Offline contract snapshot

This is a selected export set, not a complete copy of its source. It contains
only `urn:bridge-clean:grant-profile:v1` fixture bytes. `manifest.json` records
the selected bytes and their aggregate digest; `consumer-pin.json` independently
pins that manifest, the vector manifest, the trust set, generator version, and
the selected export set. `python -m contracts.verify` verifies both records
before a trust set can be loaded.

Fixture trust sets are test-only and cannot authorize production work: loading
one outside `development` is refused.

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
