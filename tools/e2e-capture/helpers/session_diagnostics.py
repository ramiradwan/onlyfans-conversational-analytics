"""Bounded exception provenance for the disposable browser test process."""

from functools import wraps
import json
from threading import Lock


METHODS = frozenset({
    "agent.storage.rotate", "agent.storage.unseal", "agent.config.get",
    "capture.state.report", "history.check.begin", "agent.challenge",
    "agent.authenticate", "agent.analysis.readiness",
})
ERRORS = frozenset({
    "CompanionSessionError", "CompanionRecordError", "AuthenticationStateError",
    "CompanionPairingPersistenceError", "LocalDataKeyError", "RuntimeError",
    "ValueError", "TypeError", "KeyError", "OperationalError", "DatabaseError",
    "IntegrityError", "TimeoutError", "ValidationError", "PermissionError",
    "InvalidToken", "InvalidTag", "OSError",
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
}
PHASES = {
    "call": "dispatch", "validate_config": "validate_config",
    "open_extension_storage_bootstrap": "open_bootstrap",
    "seal_extension_storage_bootstrap": "seal_bootstrap", "_bootstrap": "seal_bootstrap",
    "current_policy": "policy", "companion_session_policy": "policy",
    "consume_companion_ticket": "consume_config", "_ticket_binding": "ticket_binding",
}


def session_failure(method, error):
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
    return {"method": method if method in METHODS else "other", "causes": causes}


def install_session_diagnostics(endpoint, *, emit=print, limit=24):
    original = endpoint.SessionRPC.call
    remaining = min(24, max(0, limit))
    lock = Lock()

    @wraps(original)
    def call(self, method, params):
        nonlocal remaining
        try:
            return original(self, method, params)
        except Exception as error:
            try:
                with lock:
                    if remaining > 0:
                        remaining -= 1
                        emit("e2e-session-failure: " + json.dumps(session_failure(method, error), separators=(",", ":")))
            except Exception:
                pass
            raise

    endpoint.SessionRPC.call = call
