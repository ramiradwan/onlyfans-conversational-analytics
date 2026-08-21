# OnlyFans Conversational Analytics

OnlyFans Conversational Analytics is a local-first system that captures creator-visible conversation activity, processes it into structured analytics, and presents conversation, engagement, response-time, topic, and sentiment views.

> [!IMPORTANT]
> This project is an independent tool for creators. It is not affiliated with, endorsed by, or sponsored by OnlyFans or its operator. The OnlyFans trademark is used only to describe the project's compatibility and purpose.

## Components

- **Agent** — an MV3 browser extension that captures conversation data available to the logged-in creator, maintains a durable local outbox, and executes only explicitly allow-listed actions authorized through Brain.
- **Brain** — a FastAPI backend that authenticates Agent and Bridge connections, validates and persists ingestion, derives analytics and presence state, coordinates commands, and serves the local API.
- **Bridge** — a React dashboard that consumes Brain-owned snapshots and revisioned updates. It does not read Agent storage or act as an Agent transport.

Conversation processing stays in the creator-controlled local runtime. External provisioning may issue signed offline-verifiable grants, but it does not receive conversation data.

## Install and run

The product ships as a per-user Windows installer, `OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe`. It installs under `%LOCALAPPDATA%\Programs\OnlyFans Conversational Analytics` without administrator rights and needs no Python, Node.js, or repository checkout.

Released artifacts are not signed, so Microsoft Defender SmartScreen shows an unrecognized-application warning when the installer starts. Comparing digests is the available integrity check: compute the installer's SHA-256 and compare it with the digest published with the release.

```powershell
Get-FileHash -Algorithm SHA256 .\OnlyFans-Conversational-Analytics-Setup-<version>-x64.exe
```

Start **OnlyFans Conversational Analytics** from the Start Menu. `Brain.exe` starts the local runtime on `127.0.0.1:17871` and opens the browser at `http://bridge.localhost:17871`, running the configuration sequence on first launch. The Agent browser extension is added to the browser separately; the installer does not install it.

Per-user data lives in `%LOCALAPPDATA%\OnlyFans Conversational Analytics`. Uninstalling removes the program files and leaves that directory in place, because it holds the authoritative database.

- [Install and run on Windows](docs/install-windows.md) — requirements, verification, first run, data location, uninstall, and building the installer.
- [Acceptance sequence](docs/installation-and-acceptance.md) — running a built artifact through the acceptance harness on a clean guest.

## Architecture

- [Architecture decision records](docs/adr/README.md)
- [Communication specification](communication-spec.md)
- [Frontend design specification](frontend/frontend-design-spec.md)
- [Extension documentation](extension/README.md)

## Verification

```powershell
./.venv/Scripts/python -m pytest
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
cd ../extension
npm test
npm run build
npm run audit
npm run qualify:snapshot:ci
```

The 100,000-message qualification (`npm run qualify:snapshot`) and one explicitly consented, sanitized live read-only pagination run are additional Beta gates. Deterministic checks alone do not authorize a Beta declaration.
