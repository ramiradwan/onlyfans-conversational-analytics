"""Packaged process entry point with configuration-free boot dispatch."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from app.core.customer_release import (
    load_customer_release_config,
    resolve_hosted_api_origin,
)
from app.core.runtime_paths import runtime_configuration_file


PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HANDOFF_TOKEN"
PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_EXTENSION_ID"
SQLCIPHER_RUNTIME_REPORT_PATH_ENVIRONMENT_VARIABLE = "BRAIN_SQLCIPHER_RUNTIME_REPORT_PATH"
COMPANION_RUNTIME_REPORT_PATH_ENVIRONMENT_VARIABLE = "BRAIN_COMPANION_RUNTIME_REPORT_PATH"

RUNNING_APPLICATION_MUTEX_NAME = "OnlyFansConversationalAnalyticsBrain"

_running_application_mutex: int | None = None


def hold_running_application_mutex() -> bool:
    """Publish the mutex that tells the installer this application is running."""
    global _running_application_mutex
    if sys.platform != "win32":
        return False
    if _running_application_mutex is not None:
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel.CreateMutexW
    create_mutex.argtypes = (wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, False, RUNNING_APPLICATION_MUTEX_NAME)
    if not handle:
        return False
    # Bridge holds this same installer signal across backend restarts. Its
    # existence cannot identify a competing writer; the root lease does that.
    _running_application_mutex = handle
    return True


def select_brain_application(
    data_directory: str | Path | None = None,
    *,
    provisioning_completion_exit: Callable[[], None] | None = None,
):
    """Select the boot mode using only the runtime configuration file's presence."""
    if runtime_configuration_file(data_directory).exists():
        from app.main import app

        return app
    from app.provisioning.app import create_provisioning_app
    from app.provisioning.claim_submission import hosted_transport, installation_proof_authority
    from app.security.grant_refresh import configured_grant_refresh
    from app.provisioning.completion import (
        durable_authentication_store,
        durable_completion_reader,
        durable_finalize_action,
    )

    open_store = durable_authentication_store(data_directory)
    customer_release = load_customer_release_config()
    handoff_token = os.environ.get(PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE)
    hosted_origin = resolve_hosted_api_origin(customer_release)
    grant_refresh = configured_grant_refresh(
        open_store,
        hosted_origin=hosted_origin,
        transport_factory=hosted_transport,
        proof_authority_factory=installation_proof_authority,
    )
    store = open_store()
    completion_ready = durable_completion_reader(open_store, data_directory=data_directory)
    finalize_action = durable_finalize_action(open_store,
        extension_id=os.environ.get(PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE, ""),
        data_directory=data_directory, grant_refresh=grant_refresh)
    async def shutdown():
        await grant_refresh.stop()
    application = create_provisioning_app(completion_exit=provisioning_completion_exit, shutdown_action=shutdown)
    if handoff_token is not None:
        from app.onboarding.composition import attach_desktop_setup, build_desktop_setup

        core = build_desktop_setup(store=store, stage="setup",
            extension_id=os.environ.get(PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE),
            hosted_origin=hosted_origin, authentication_providers=customer_release.authentication_providers,
            finalize=finalize_action, completion_ready=completion_ready,
            completed=application.state.request_setup_completion)
        coordinator = attach_desktop_setup(application, core=core, launcher_token=handoff_token)
        if core.recovery.initial is not None and customer_release.hosted_onboarding_url:
            from app.provisioning.receiving import install_receiving
            from app.provisioning.setup_transfer import DesktopSetupTransfer
            install_receiving(application, coordinator, DesktopSetupTransfer(core.recovery.initial, hosted_setup_url=customer_release.hosted_onboarding_url))
    return application


def supervised_server(configuration, application):
    """End presentation responses before Uvicorn drains owned application work."""
    import uvicorn

    class OwnedServer(uvicorn.Server):
        async def shutdown(self, sockets=None):
            coordinator = getattr(application.state, "desktop_coordinator", None)
            if coordinator is not None:
                coordinator.begin_shutdown()
            await super().shutdown(sockets=sockets)

    return OwnedServer(configuration)


class ObservedApplication:
    """Observe ASGI lifecycle completion without changing domain shutdown owners."""

    def __init__(self, application, status, stopped):
        self.application, self.status, self.stopped = application, status, stopped
        self.drained = False

    async def __call__(self, scope, receive, send):
        async def observed_send(message):
            if message["type"] == "lifespan.startup.complete":
                self.status.emit("ready-to-check-listener")
                if self.stopped.is_set():
                    await send({"type": "lifespan.startup.failed", "message": "startup_cancelled"})
                    return
            await send(message)
            if message["type"] == "lifespan.shutdown.complete":
                self.drained = getattr(self.application.state, "runtime_shutdown_drained", False) is True
            elif message["type"] == "lifespan.shutdown.failed":
                self.drained = False
        try:
            await self.application(scope, receive, observed_send if scope["type"] == "lifespan" else send)
        except BaseException:
            self.drained = False
            raise


def run_brain() -> int:
    """Run exactly one fixed-origin ASGI worker for the selected boot mode."""
    import threading
    from app.core.process_lifetime import parent_lifetime
    from app.core.bootstrap_status import bootstrap_status
    from app.persistence.migrations import startup_migration_supervision
    from app.core.startup_maintenance import acquire_runtime_ownership, admit_retained_schemas, StartupRefused

    stopped = threading.Event()
    server: uvicorn.Server | None = None

    def request_exit() -> None:
        stopped.set()
        if server is not None:
            server.should_exit = True
    # Observe the parent before root inspection or a migrating constructor.
    with parent_lifetime(request_exit), bootstrap_status(stopped) as status, startup_migration_supervision(status.cancelled):
        if stopped.is_set():
            return 1
        try:
            state = acquire_runtime_ownership()
        except StartupRefused as error:
            status.emit("recovery-required", "writer-conflict" if str(error) == "runtime_writer_conflict" else "retained-data")
            raise
        if stopped.is_set():
            return 1
        try:
            admit_retained_schemas(state, should_cancel=status.cancelled, on_phase=status.maintenance_phase)
        except StartupRefused:
            if stopped.is_set():
                return 1
            status.emit("recovery-required", "maintenance")
            raise
        if stopped.is_set():
            return 1
        import uvicorn
        status.emit("validating")
        application = select_brain_application(provisioning_completion_exit=request_exit)
        if stopped.is_set():
            return 1

        observed_application = ObservedApplication(application, status, stopped)

        configuration = uvicorn.Config(
            observed_application, host="127.0.0.1", port=17871, workers=1, access_log=False, lifespan="on",
            ws_max_size=36_864, ws_max_queue=8, ws_per_message_deflate=False,
        )
        server = supervised_server(configuration, application)
        if stopped.is_set():
            server.should_exit = True
        server.run()
        if status.phase == "ready-to-check-listener":
            status.shutdown_complete(observed_application.drained)
    return 75 if state.mode != "configured" and application.state.completion_requested else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Start only an internal Brain process or runtime probe."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == ("--companion-runtime-report",):
        import json
        from app.security.companion_noise import qualification_report

        rendered_report = json.dumps(qualification_report(), sort_keys=True)
        report_path = os.environ.get(COMPANION_RUNTIME_REPORT_PATH_ENVIRONMENT_VARIABLE)
        if report_path:
            Path(report_path).write_text(rendered_report + "\n", encoding="utf-8")
        if sys.stdout is not None:
            print(rendered_report)
        return 0
    if arguments == ("--sqlcipher-runtime-report",):
        import json
        from app.persistence.sqlcipher_runtime import qualification_report

        rendered_report = json.dumps(qualification_report(), sort_keys=True)
        report_path = os.environ.get(SQLCIPHER_RUNTIME_REPORT_PATH_ENVIRONMENT_VARIABLE)
        if report_path:
            Path(report_path).write_text(rendered_report + "\n", encoding="utf-8")
        print(rendered_report)
        return 0
    published = hold_running_application_mutex()
    if arguments == ("--brain",):
        if sys.platform == "win32" and not published:
            return 78
        from app.core.startup_maintenance import StartupRefused
        try:
            return run_brain()
        except StartupRefused as error:
            # The status pipe already reports the bounded recovery reason.
            # An expected refusal must terminate a windowed frozen process,
            # rather than enter PyInstaller's unhandled-exception dialog.
            if sys.stderr is not None:
                import re
                reason = str(error)
                print(reason if re.fullmatch(r"[a-z][a-z_]{0,95}", reason) else "startup_refused", file=sys.stderr)
            return 78
    raise SystemExit("Open Conversation Analytics using Bridge.exe. Brain.exe is an internal service.")


if __name__ == "__main__":
    raise SystemExit(main())
