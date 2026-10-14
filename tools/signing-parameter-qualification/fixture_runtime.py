"""Explicit frozen QA entry; never the shipping Brain entry.

Uses ordinary app.main lifecycle and authorization. Its two declared composition
substitutions are pinned public commercial fixture trust and a software fixture
installation provider. Grant/publication producers are never imported. The local
operator is pre-admitted; this gate does not qualify WebAuthn or TPM hardware.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fixture_installation import open_admitted_context, open_fixture_provider


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main():
    import uvicorn
    from fastapi import HTTPException, Request, Response
    from contracts.loader import load_trust_set
    from app.core.config import settings
    from app.security import activation_gate
    from app.security.account_bindings import eligible_account_for_policy
    from app.api.activation import require_activated_runtime

    context = open_admitted_context(os.environ["LOCAL_ANALYTICS_DATA_DIR"])

    def public_fixture_trust(path, **kwargs):
        if path != activation_gate.PRODUCTION_TRUST_SET_PATH:
            raise ValueError("Qualification trust path refused")
        raw = load_trust_set("grant-profile-v1/keys/trust-set.json", environment="development")
        if raw.get("production_usable") is not False or not all(key.get("fixture_only") is True for key in raw["keys"]):
            raise ValueError("Qualification trust requires pinned public fixture material")
        return {**raw, "profile": activation_gate.GRANT_PROFILE, "transition_sequence": 1}

    # Explicit QA composition, not a replacement activation decision. Every
    # production condition below still evaluates its ordinary implementation.
    activation_gate.load_pinned_trust_set = public_fixture_trust
    activation_gate.WindowsCNGInstallationKeyProvider = lambda: open_fixture_provider(context.store)
    from app import main as application
    from app.api.endpoints import companion_pairing, history
    application.WindowsCNGInstallationKeyProvider = lambda: open_fixture_provider(context.store)
    companion_pairing.WindowsCNGInstallationKeyProvider = lambda: open_fixture_provider(context.store)
    from app.provisioning import claim_submission
    claim_submission.WindowsCNGInstallationKeyProvider = lambda: open_fixture_provider(context.store)
    from app.bootstrap import transport_manager as manager
    from app.models.history import UpdateHistorySettingsRequest
    from app.security.companion_noise import qualification_report

    loop = []

    @application.app.on_event("startup")
    async def capture_loop():
        loop.append(asyncio.get_running_loop())

    server = uvicorn.Server(uvicorn.Config(
        application.app, host="127.0.0.1", port=17871, log_level="critical", access_log=False,
        ws_max_size=36_864, ws_max_queue=8, ws_per_message_deflate=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("Qualification listener unavailable")
        time.sleep(0.02)
    pairing_id = None

    def current_policy():
        require_activated_runtime()
        return context.refresh_policy()

    def state():
        decision = activation_gate.current_activation()
        desired = manager.history.history_settings(context.account)
        coverage = manager.history.coverage(context.account)
        with manager.history.database.read() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM account_messages WHERE creator_account_id=?", (context.account,)).fetchone()[0])
        try:
            context.refresh_policy()
            grant_status = "current"
        except ValueError:
            grant_status = "refused"
        return {
            "activation": {outcome.condition: outcome.satisfied for outcome in decision.outcomes},
            "activated": decision.activated, "production_checks": decision.production,
            "history_settings_revision": desired["settings_revision"],
            "desired_state": desired["desired_state"], "effective_state": desired["effective_state"],
            "canonical_messages": count, "coverage_status": coverage["status"], "coverage_phase": coverage["phase"],
            "capture_counters": manager.catchup.counters(),
            "grant_status": grant_status,
        }

    async def configure_history(enabled):
        policy = current_policy()
        desired = manager.history.history_settings(context.account)
        request = Request({"type": "http", "method": "PUT", "path": "/api/v1/settings/history",
                           "headers": [(b"origin", settings.bridge_origin.encode("ascii")),
                                       (b"host", urlsplit(settings.bridge_origin).netloc.encode("ascii"))],
                           "scheme": "http", "server": ("bridge.localhost", 17871), "client": ("127.0.0.1", 1), "query_string": b""})
        model = UpdateHistorySettingsRequest(
            desired_state="running" if enabled else "paused",
            consent_policy_version=desired["consent_policy_version"], accept_consent=desired["consent_revision"] is None,
            recent_window_days=365, page_size=100, pages_per_wake=1, request_interval_ms=1000, retry_limit=2,
        )
        await history.update_history_settings(request, model, Response(), policy,
                                              if_match=f'"{desired["settings_revision"]}"', csrf=context.csrf_value)
        return state()

    try:
        readiness = state()
        if not readiness["activated"] or len(readiness["activation"]) != 6 or not all(readiness["activation"].values()):
            raise RuntimeError("Qualification activation refused")
        emit({"ready": True, "qualification_only": True, "native": qualification_report(), **readiness})
        for line in sys.stdin:
            request = json.loads(line)
            operation = request.get("operation")
            # These are public coarse diagnostics and owned process shutdown,
            # not account work. Expiry must not turn a diagnostic poll into a
            # substitute for the shipping app's independent authority watcher.
            if operation == "state":
                emit(state())
                continue
            if operation == "stop":
                emit({"stopped": True})
                break
            try:
                policy = current_policy()
            except (HTTPException, ValueError):
                emit({"refused": True, "reason": "authority"})
                continue
            if operation == "configure":
                value = request["extension_id"]
                if len(value) != 32 or any(c not in "abcdefghijklmnop" for c in value):
                    raise ValueError("Qualification extension refused")
                settings.extension_id = value
                result = {"configured": True}
            elif operation == "open":
                result = context.service.open(policy, context.account)
                pairing_id = result["pairing_id"]
            elif operation == "status":
                result = context.service.status(policy, pairing_id)
            elif operation == "confirm":
                status = context.service.status(policy, pairing_id)
                if status["state"] != "awaiting_confirmation" or status["comparison_code"] != request["comparison_code"]:
                    raise ValueError("Qualification comparison refused")
                result = context.service.confirm(policy, pairing_id, status["version"])
            elif operation in {"history_settings", "pause", "resume"}:
                enabled = request.get("enabled", True) if operation == "history_settings" else operation == "resume"
                result = asyncio.run_coroutine_threadsafe(configure_history(bool(enabled)), loop[0]).result(timeout=15)
            else:
                raise ValueError("Qualification command refused")
            emit(result)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            raise RuntimeError("Qualification runtime did not drain")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        print(json.dumps({"error_type": type(error).__name__,
                          "frames": [{"file": os.path.basename(frame.filename), "function": frame.name,
                                      "line": frame.lineno} for frame in frames]}), file=sys.stderr, flush=True)
        emit({"error": "installed_qualification_peer_failed"})
        raise SystemExit(1) from None
