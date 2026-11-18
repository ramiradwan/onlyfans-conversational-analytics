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
from app.security.browser_ceremony_replacement import BrowserCeremonyReplacementClient
from app.security.browser_ceremony_wait import BrowserCeremonyWaitClient
from app.security.desktop_decision import DesktopDecisionClient
from app.security.source_replacement import SourceReplacementClient, INITIAL, REGISTERED

SIGN_IN_KEEPALIVE_SECONDS = 60.0


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
                        finalize, completion_ready, completed=lambda: None, adapt_owner=None,
                        protected_recovery_enabled=False):
    """Construct common services; the runtime root supplies its authority adapter."""
    if (stage == "runtime") != (adapt_owner is not None):
        raise ValueError("Runtime setup requires its authority adapter")
    if type(protected_recovery_enabled) is not bool or protected_recovery_enabled and not hosted_origin:
        raise ValueError("Protected recovery qualification requires the configured Provider")
    admissions = ScopedAdmissions(store, stage=stage)
    tasks = BrowserTasks(admissions, extension_id=extension_id or None)
    key = installation_proof_authority(store)
    ceremonies = initial = registered = None
    source_replacement_clients = {}
    if hosted_origin:
        # One pool keeps a sign-in's proof exchanges on an open connection.
        proofs = hosted_transport(hosted_origin, keepalive_seconds=SIGN_IN_KEEPALIVE_SECONDS)
        ceremony_client = BrowserCeremonyClient(proofs, key)
        ceremonies = BrowserCeremonies(store,
            ceremony_client, hosted_origin=hosted_origin,
            replacement_client=BrowserCeremonyReplacementClient(ceremony_client),
            decision_client=DesktopDecisionClient(proofs, key),
            authentication_client=BrowserAuthenticationClient(proofs, key),
            authentication_providers=authentication_providers,
            wait_client=BrowserCeremonyWaitClient(proofs, key, hosted_origin))
        # The first sign-in exchange is the initial handoff, so it shares the proof pool.
        initial = InitialInstallationEnrollment(store, hosted_origin=hosted_origin, transport=proofs)
        registered = RegisteredInstallationContinuation(store, hosted_origin=hosted_origin)
        source_replacement_clients = {
            "initial-enrollment": SourceReplacementClient(INITIAL, proofs, key, clock=store._now),
            "registered-continuation": SourceReplacementClient(REGISTERED, registered.client.transport, key, clock=store._now),
        }
    recovery = ProvisioningSetupOwner(store, browser_tasks=tasks, ceremonies=ceremonies,
        initial=initial, registered=registered, key=key, finalize=finalize,
        completion_ready=completion_ready, completed=completed,
        source_replacement_clients=source_replacement_clients)
    if initial is not None:
        initial.authorize_scope = recovery.authorize_initial_scope
    owner = recovery if adapt_owner is None else adapt_owner(recovery)
    # The public boot graphs do not opt in yet. This composition seam is for
    # bounded qualification until the protected sign-in/launch UI is connected.
    # A disabled boot with a retained admission must still fence that command.
    with store.database.read() as connection:
        retained = connection.execute("SELECT 1 FROM protected_recovery_desktop_admissions LIMIT 1").fetchone() is not None
    if protected_recovery_enabled or retained:
        from app.onboarding.protected_authority_setup import ProtectedAuthoritySetupOwner
        components = {}
        if protected_recovery_enabled:
            from app.onboarding.protected_browser_recovery import ProtectedBrowserRecoveryEffects
            from app.persistence.installation_authority_recovery import InstallationAuthorityRecoveries
            from app.persistence.protected_browser_recovery import ProtectedBrowserRecoveries
            from app.security.hosted_grants import HostedGrantClient
            from app.security.installation_authority_recovery import InstallationAuthorityRecoveryClient
            from app.security.protected_browser_recovery import ProtectedCeremonyClient, ProtectedAuthenticationClient
            journal = InstallationAuthorityRecoveries(store, origin=hosted_origin)
            browser = ProtectedBrowserRecoveryEffects(ProtectedBrowserRecoveries(journal),
                ProtectedCeremonyClient(proofs, key, clock=store._now),
                ProtectedAuthenticationClient(proofs, key, clock=store._now))
            components = dict(recovery=journal, browser=browser,
                client=InstallationAuthorityRecoveryClient(proofs, key, clock=store._now),
                verifier=HostedGrantClient(proofs, key, store, clock=store._now))
        owner = ProtectedAuthoritySetupOwner(owner, store, **components)
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
    def notices(session):
        return tasks.helpers.notices({task["task_id"] for task in tasks.current_native(session)})

    warm = None if ceremonies is None else getattr(ceremonies.client.transport, "warm", None)
    application.include_router(desktop_router(coordinator, actor=actor, next_expiry=expiry, notices=notices, warm=warm))
    application.include_router(browser_router(coordinator, tasks, ceremonies, pairing=pairing))
    application.include_router(setup_surface_router())
    return coordinator
