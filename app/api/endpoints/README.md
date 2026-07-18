# API endpoints

This package contains the FastAPI route definitions for Brain.

## `transport_ws.py`

Implements the role-specific protocol-v1 transport:

- `/ws/agent` accepts Agent hello, ingestion, presence, configuration-state, and command-result messages.
- `/ws/bridge` accepts Bridge hello and state-resynchronization messages.
- `GET /api/v1/agent/config` returns the authenticated immutable Agent configuration document and supports ETag validation.

Socket role and creator-account identity are bound during the hello exchange. Wrong-role, pre-handshake, identity-conflicting, and unauthorized messages are rejected according to the protocol contracts.

## `frontend.py`

- `GET /` serves the compiled Bridge assets and injects local runtime configuration.
- `GET /api/v1/frontend/bootstrap/{user_id}` returns the frontend bootstrap snapshot.

## `insights.py`

Provides analytics queries under `/api/v1/insights`:

- `GET /topics`
- `GET /sentiment-trend`
- `GET /response-time`
- `GET /full`

Request and response payloads use the models in `app/models`.

## `schema.py`

- `GET /api/v1/schemas/wss` exposes the WebSocket response schema used by frontend type-generation tooling.

## Responsibilities

Endpoint modules validate transport inputs, invoke the relevant service or repository boundary, and return typed HTTP or WebSocket responses. Domain sequencing, deduplication, configuration immutability, and command lifecycle rules remain in the service layer.
