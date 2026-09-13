<!-- CODE-VERIFY: Verify snapshot contents, integrity checks, trust-set behavior, regeneration commands, and versioning rules against contracts tooling before editing. -->

# Offline contract snapshot

`contracts/` contains a selected offline snapshot of external contract material. It is not a complete copy of its source.

`manifest.json` records the selected files and their digests. `consumer-pin.json` independently pins the manifest, supported profiles, trust sets, vector/conformance manifests, generator versions, and the exact upstream repository commit/tree plus published source-manifest digest.

Verify the snapshot before using it:

```powershell
python -m contracts.verify
```

Trust sets that are not marked for production use are rejected outside the development environment.

## Update the snapshot

Regenerate the snapshot only from the approved `ramiradwan/creator-platform-contracts` source checkout pinned by `consumer-pin.json`:

```powershell
python tools/regenerate_contract_snapshot.py --copy-from <approved-source-checkout>
python tools/regenerate_contract_snapshot.py --check
python -m contracts.verify
```

The generator verifies the pinned published source manifest and exact approved export digests before copying bytes. Do not hand-edit vendored fixtures, manifests, pins, or hashes. Released `v1` contract bytes are append-only; a semantic change requires a new profile-version directory.
