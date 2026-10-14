"""Restricted setup process: native presentation and explicit scoped helpers."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.provisioning.events import events


def create_provisioning_app(*, completion_exit=None, shutdown_action=None):
    @asynccontextmanager
    async def lifecycle(application):
        coordinator = getattr(application.state, "desktop_coordinator", None)
        try:
            if coordinator is not None:
                coordinator.start()
            yield
        finally:
            try:
                if coordinator is not None:
                    await application.state.desktop_setup_core.close()
            finally:
                if shutdown_action is not None:
                    await shutdown_action()

    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifecycle)
    application.state.completion_requested = False

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    def request_completion_exit():
        application.state.completion_requested = True
        events.publish()
        if completion_exit is not None:
            completion_exit()

    application.state.request_setup_completion = request_completion_exit
    return application
