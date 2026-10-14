"""Owned desktop setup construction, isolated from the configured runtime graph."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.onboarding.admission_routes import admission_router
from app.onboarding.admission import ScopedAdmissions
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.ceremonies import BrowserCeremonies
from app.onboarding.browser_routes import browser_router
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.onboarding.routes import desktop_router
from app.onboarding.surface import setup_surface_router
from app.provisioning.state import next_authority_expiry
from app.provisioning.claim_submission import hosted_transport, installation_proof_authority
from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
from app.provisioning.initial_handoff import InitialInstallationEnrollment
from app.provisioning.installation_continuation import RegisteredInstallationContinuation
from app.security.browser_authentication import BrowserAuthenticationClient
from app.security.browser_ceremony import BrowserCeremonyClient
from app.security.desktop_decision import DesktopDecisionClient


@dataclass
class DesktopSetupCore:
    admissions: ScopedAdmissions
    tasks: BrowserTasks
    ceremonies: BrowserCeremonies | None
    recovery: ProvisioningSetupOwner
    coordinator: DesktopOnboardingCoordinator
    _close_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    async def close(self):
        """Drain the owned graph once, even if a shutdown waiter is cancelled."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        if self._close_task.done():
            self._close_task.result()
            return
        await asyncio.shield(self._close_task)

    async def _close(self):
        try:
            await self.coordinator.stop()
        finally:
            try:
                await self.coordinator.owner.stop()
            finally:
                # Runtime activation owns its ceremony clients; restricted
                # provisioning has no activation adapter to close them.
                if self.admissions.stage == "setup" and self.ceremonies is not None:
                    await self.ceremonies.stop()


def build_desktop_setup(*, store, stage, extension_id, hosted_origin, authentication_providers,
                        finalize, completion_ready, completed=lambda: None, adapt_owner=None):
    """Construct common services; the runtime root supplies its authority adapter."""
    if (stage == "runtime") != (adapt_owner is not None):
        raise ValueError("Runtime setup requires its authority adapter")
    admissions = ScopedAdmissions(store, stage=stage)
    tasks = BrowserTasks(admissions, extension_id=extension_id or None)
    key = installation_proof_authority(store)
    ceremonies = initial = registered = None
    if hosted_origin:
        ceremonies = BrowserCeremonies(store,
            BrowserCeremonyClient(hosted_transport(hosted_origin), key), hosted_origin=hosted_origin,
            decision_client=DesktopDecisionClient(hosted_transport(hosted_origin), key),
            authentication_client=BrowserAuthenticationClient(hosted_transport(hosted_origin), key),
            authentication_providers=authentication_providers)
        initial = InitialInstallationEnrollment(store, hosted_origin=hosted_origin)
        registered = RegisteredInstallationContinuation(store, hosted_origin=hosted_origin)
    recovery = ProvisioningSetupOwner(store, browser_tasks=tasks, ceremonies=ceremonies,
        initial=initial, registered=registered, key=key, finalize=finalize,
        completion_ready=completion_ready, completed=completed)
    if initial is not None:
        initial.authorize_scope = recovery.authorize_initial_scope
    owner = recovery if adapt_owner is None else adapt_owner(recovery)
    return DesktopSetupCore(admissions, tasks, ceremonies, recovery,
        DesktopOnboardingCoordinator(admissions, owner))


def attach_desktop_setup(application, *, core, launcher_token,
                         actor=lambda request, mutation: None, pairing=None):
    admissions, tasks, ceremonies = core.admissions, core.tasks, core.ceremonies
    coordinator = core.coordinator
    application.state.desktop_setup_core = core
    application.state.desktop_admissions = admissions
    application.state.desktop_coordinator = coordinator
    application.state.desktop_setup_owner = coordinator.owner

    def expiry():
        values = [value for value in (tasks.next_expiry(), next_authority_expiry(admissions.authentication)) if value is not None]
        return min(values, default=1800.0)

    application.include_router(admission_router(admissions, launcher_token=launcher_token))
    application.include_router(desktop_router(coordinator, actor=actor, next_expiry=expiry))
    application.include_router(browser_router(coordinator, tasks, ceremonies, pairing=pairing))
    application.include_router(setup_surface_router())
    return coordinator
