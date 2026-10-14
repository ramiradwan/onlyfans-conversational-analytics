"""Configured composition of desktop setup; mounted before frontend navigation."""
from pathlib import Path

from app.api.security import get_runtime_policy, verify_csrf_token
from app.bootstrap import transport_manager
from app.core.config import settings
from app.core.customer_release import load_customer_release_config, resolve_hosted_api_origin
from app.onboarding.activation import DesktopActivation
from app.onboarding.composition import attach_desktop_setup, build_desktop_setup
from app.onboarding.companion import companion_task_router
from app.onboarding.pairing import DesktopPairing
from app.onboarding.runtime import RuntimeActor, RuntimeSetupOwner
from app.persistence.auth import SQLiteAuthenticationStore
from app.provisioning.completion import durable_completion_reader, durable_finalize_action
from app.provisioning.installation_continuation import RegisteredInstallationContinuation
from app.security.capability_license_redemption import durable_capability_license_opaque_redemption
from app.security.companion_pairing import CompanionPairingService


def configure_desktop_setup(application, *, launcher_token, grant_refresh=lambda: None):
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    release = load_customer_release_config()
    origin = resolve_hosted_api_origin(release)
    directory = Path(settings.auth_database_path).parent

    def finalize(**request):
        return durable_finalize_action(lambda: store, extension_id=settings.extension_id,
            data_directory=directory, grant_refresh=grant_refresh())(**request)

    def runtime_owner(recovery):
        activation = None
        if origin:
            activation = DesktopActivation(store, key=recovery.key,
                registered=RegisteredInstallationContinuation(store, hosted_origin=origin, retain_current_binding=True),
                ceremonies=recovery.ceremonies, browser_tasks=recovery.browser_tasks,
                redemption=durable_capability_license_opaque_redemption(lambda: store, hosted_origin=origin))
        pairing = DesktopPairing(store, browser_tasks=recovery.browser_tasks,
            service=CompanionPairingService(store, recovery.key))
        return RuntimeSetupOwner(store, browser_tasks=recovery.browser_tasks,
            activation=activation, pairing=pairing, setup_owner=recovery)

    core = build_desktop_setup(store=store, stage="runtime", extension_id=settings.extension_id,
        hosted_origin=origin, authentication_providers=release.authentication_providers,
        finalize=finalize, completion_ready=durable_completion_reader(lambda: store, data_directory=directory),
        adapt_owner=runtime_owner)

    def actor(request, mutation):
        def policy():
            current = get_runtime_policy(request)
            if mutation:
                verify_csrf_token(current, request.headers.get("X-CSRF-Token"))
            return current
        return RuntimeActor(policy)

    coordinator = attach_desktop_setup(application, core=core, launcher_token=launcher_token,
        actor=actor, pairing=core.coordinator.owner.pairing)
    application.include_router(companion_task_router(coordinator, core.tasks, core.ceremonies,
        manager=transport_manager, actor=actor))
    return coordinator
