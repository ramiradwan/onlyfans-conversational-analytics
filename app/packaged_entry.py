"""Packaged process entry point with configuration-free boot dispatch."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from app.core.runtime_paths import runtime_configuration_file


PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HANDOFF_TOKEN"
PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_EXTENSION_ID"
PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HOSTED_ORIGIN"

# The Windows installer reads this name from its AppMutex directive to detect a
# running instance. Both process modes publish it, and changing it here requires
# the same change in packaging/inno/brain.iss.
RUNNING_APPLICATION_MUTEX_NAME = "OnlyFansConversationalAnalyticsBrain"

_running_application_mutex: int | None = None


def hold_running_application_mutex() -> bool:
    """Publish the mutex that tells the installer this application is running.

    The handle is kept for the lifetime of the process and never released, so
    the mutex disappears only when the process does.
    """
    global _running_application_mutex
    if sys.platform != "win32":
        return False
    if _running_application_mutex is not None:
        return True
    import ctypes
    from ctypes import wintypes

    create_mutex = ctypes.windll.kernel32.CreateMutexW
    create_mutex.argtypes = (wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    handle = create_mutex(None, False, RUNNING_APPLICATION_MUTEX_NAME)
    if not handle:
        return False
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
    from app.provisioning.claim_submission import durable_claim_submission
    from app.provisioning.creator_association import (
        durable_creator_association_initiation,
    )
    from app.provisioning.binding_acquisition import (
        durable_creator_account_binding_acquisition,
    )
    from app.provisioning.completion import (
        durable_authentication_store,
        durable_completion_reader,
        durable_finalize_action,
    )

    open_store = durable_authentication_store(data_directory)
    return create_provisioning_app(
        claim_submission=durable_claim_submission(
            open_store,
            hosted_origin=os.environ.get(
                PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, ""
            ),
        ),
        creator_association_initiation=durable_creator_association_initiation(
            open_store,
            hosted_origin=os.environ.get(
                PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, ""
            ),
        ),
        creator_binding_acquisition=durable_creator_account_binding_acquisition(
            open_store,
            hosted_origin=os.environ.get(
                PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, ""
            ),
        ),
        completion_ready=durable_completion_reader(
            open_store, data_directory=data_directory
        ),
        finalize_action=durable_finalize_action(
            open_store,
            extension_id=os.environ.get(
                PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE, ""
            ),
            data_directory=data_directory,
        ),
        extension_id=os.environ.get(PROVISIONING_EXTENSION_ID_ENVIRONMENT_VARIABLE, ""),
        launcher_handoff_token=os.environ.get(PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE),
        completion_exit=provisioning_completion_exit,
    )


def run_brain() -> int:
    """Run exactly one fixed-origin ASGI worker for the selected boot mode."""
    import uvicorn

    if runtime_configuration_file().exists():
        application = select_brain_application()
        configuration = uvicorn.Config(
            application, host="127.0.0.1", port=17871, workers=1, access_log=False
        )
        uvicorn.Server(configuration).run()
        return 0
    server: uvicorn.Server | None = None

    def request_exit() -> None:
        if server is not None:
            server.should_exit = True

    application = select_brain_application(
        provisioning_completion_exit=request_exit,
    )
    configuration = uvicorn.Config(
        application, host="127.0.0.1", port=17871, workers=1, access_log=False
    )
    server = uvicorn.Server(configuration)
    server.run()
    return 75 if application.state.completion_requested else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Start the local launcher or the internal Brain process mode."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    hold_running_application_mutex()
    if arguments == ("--brain",):
        return run_brain()
    if arguments:
        raise SystemExit("usage: Brain.exe [--brain]")
    from app.launcher import main as launcher_main

    return launcher_main()


if __name__ == "__main__":
    raise SystemExit(main())
