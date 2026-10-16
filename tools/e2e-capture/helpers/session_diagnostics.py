"""Bounded exception provenance for the disposable browser test process."""

from functools import wraps
import json
import math
from threading import Lock


METHODS = frozenset({
    "agent.storage.rotate", "agent.storage.unseal", "agent.config.get",
    "capture.state.report", "history.check.begin", "agent.challenge",
    "agent.authenticate", "agent.analysis.readiness", "session.serve", "session.check",
})
ERRORS = frozenset({
    "CompanionSessionError", "CompanionRecordError", "AuthenticationStateError",
    "CompanionPairingPersistenceError", "LocalDataKeyError", "RuntimeError",
    "ValueError", "TypeError", "KeyError", "OperationalError", "DatabaseError",
    "IntegrityError", "TimeoutError", "ValidationError", "PermissionError",
    "InvalidToken", "InvalidTag", "OSError", "QueueFull", "WebSocketDisconnect",
})
MODULES = {
    "app.api.endpoints.companion_session": "rpc",
    "app.security.companion_session_authority": "authority",
    "app.persistence.auth": "auth_store",
    "app.persistence.companion_pairing": "pairing_store",
    "app.security.extension_storage": "bootstrap",
    "app.security.local_data_key": "data_key",
    "app.persistence.catchup": "catchup",
    "app.transport.manager": "manager",
    "app.transport.companion_channel": "channel",
    "app.api.endpoints.transport_ws": "agent_socket",
}
PHASES = {
    "call": "dispatch", "validate_config": "validate_config",
    "open_extension_storage_bootstrap": "open_bootstrap",
    "seal_extension_storage_bootstrap": "seal_bootstrap", "_bootstrap": "seal_bootstrap",
    "current_policy": "policy", "companion_session_policy": "policy",
    "consume_companion_ticket": "consume_config", "_ticket_binding": "ticket_binding",
    "_serve": "serve", "read": "receive", "receive": "receive", "send": "send",
    "_agent_socket": "agent_socket", "broadcast_catchup": "broadcast_catchup",
    "check": "admission",
}


def _channel_admission(error):
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if (frame.f_globals.get("__name__") == "app.transport.companion_channel"
                and frame.f_code.co_name == "check"):
            local = frame.f_locals
            owner = vars(local["self"])
            values = [local.get("now"), local.get("wall"), owner.get("last_clock"),
                      owner.get("last_wall"), owner.get("deadline"), owner.get("expires_at")]
            if (type(owner.get("closed")) is not bool
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
                return None
            now, wall, previous, previous_wall, deadline, expires = values
            return {"closed": owner["closed"], "monotonicRollback": now < previous,
                    "wallRollback": wall < previous_wall, "deadlinePassed": now >= deadline,
                    "grantExpired": wall >= expires}
        trace = trace.tb_next
    return None


def session_failure(method, error):
    admission = _channel_admission(error) if method == "session.check" else None
    causes, seen = [], set()
    while error is not None and id(error) not in seen and len(causes) < 4:
        seen.add(id(error))
        frames, phase = [], "other"
        trace = error.__traceback__
        while trace is not None:
            module = MODULES.get(trace.tb_frame.f_globals.get("__name__"))
            if module is not None:
                frames.append({"module": module, "line": trace.tb_lineno})
                frames = frames[-8:]
                phase = PHASES.get(trace.tb_frame.f_code.co_name, phase)
            trace = trace.tb_next
        name = type(error).__name__
        causes.append({"errorName": name if name in ERRORS else "other", "phase": phase, "frames": frames})
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    result = {"method": method if method in METHODS else "other", "causes": causes}
    if method == "session.check":
        result["admission"] = admission
    return result


def install_session_diagnostics(endpoint, *, emit=print, limit=24):
    original = endpoint.SessionRPC.call
    remaining = min(24, max(0, limit))
    lock = Lock()

    def record(method, error):
        nonlocal remaining
        try:
            with lock:
                if remaining > 0:
                    remaining -= 1
                    emit("e2e-session-failure: " + json.dumps(session_failure(method, error), separators=(",", ":")))
        except Exception:
            pass

    @wraps(original)
    def call(self, method, params):
        try:
            return original(self, method, params)
        except Exception as error:
            record(method, error)
            raise

    endpoint.SessionRPC.call = call
    channel_type = getattr(endpoint, "CompanionChannel", None)
    if channel_type is not None:
        original_check = channel_type.check

        @wraps(original_check)
        def check(self):
            try:
                return original_check(self)
            except Exception as error:
                # Snapshot before cleanup closes the channel. No extra clock
                # reads or retained raw times enter the diagnostic.
                record("session.check", error)
                raise

        channel_type.check = check
    original_serve = getattr(endpoint, "_serve", None)
    if original_serve is not None:
        @wraps(original_serve)
        async def serve(channel, pin):
            try:
                return await original_serve(channel, pin)
            except Exception as error:
                record("session.serve", error)
                raise
        endpoint._serve = serve
