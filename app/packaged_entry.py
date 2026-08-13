"""Packaged process entry point with configuration-free boot dispatch."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from app.core.runtime_paths import runtime_configuration_file


PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HANDOFF_TOKEN"


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

    return create_provisioning_app(
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
    if arguments == ("--brain",):
        return run_brain()
    if arguments:
        raise SystemExit("usage: Brain.exe [--brain]")
    from app.launcher import main as launcher_main

    return launcher_main()


if __name__ == "__main__":
    raise SystemExit(main())
