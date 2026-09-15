"""Keep the Agent socket origin free of Bridge HTTP and cookie issuance."""

AGENT_HOST = b"127.0.0.1:17871"
AGENT_PATHS = frozenset({"/ws/agent", "/ws/agent/pairing"})


class CompanionOriginBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = scope.get("headers", ())
        hosts = [value.lower() for name, value in headers if name.lower() == b"host"]
        on_agent_host = AGENT_HOST in hosts
        if on_agent_host and (len(hosts) != 1 or scope["type"] == "http"):
            if scope["type"] == "http":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"cache-control", b"no-store")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
            else:
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "session_refused",
                    }
                )
            return
        if (
            on_agent_host
            and scope["type"] == "websocket"
            and scope.get("path") not in AGENT_PATHS
        ):
            await send(
                {"type": "websocket.close", "code": 1008, "reason": "session_refused"}
            )
            return
        await self.app(scope, receive, send)
