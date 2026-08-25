# Contributing

Keep changes focused and preserve the repository's architecture and data boundaries.

## Set up

CI uses Python 3.11 and Node.js 22. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
npm ci --prefix frontend
npm ci --prefix extension
```

Use the equivalent virtual-environment path on non-Windows systems.

## Before changing code

- Read the relevant [architecture decisions](docs/adr/README.md) before changing system boundaries or protocol behavior.
- Treat accepted ADRs as authoritative. Update or supersede an ADR when the architecture changes.
- Do not edit generated assets or generated contracts by hand. Use their owning build or generation step.
- Keep conversation data, credentials, raw platform responses, and other sensitive material out of logs and fixtures.
- Preserve unrelated work when editing an existing checkout.

## Test changes

Run the checks that cover your change. See [Test changes](docs/testing.md) for the common commands and CI coverage.

## Write documentation

Follow the [documentation style](docs/style.md). Keep docs short, neutral, and focused on one task or concept.

## Submit changes

Keep commits and pull requests limited to one coherent change. Explain behavior changes, test coverage, and any architecture decision that was added or updated.
