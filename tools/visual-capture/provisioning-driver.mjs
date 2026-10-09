import { readFile } from 'node:fs/promises';

export async function installProvisioningFixture(page, { stage = 'registration_required', name = 'connect', deliveryDelay = 0, acquireRefusal = null } = {}) {
  const html = (await readFile(new URL('../../app/provisioning/provisioning.html', import.meta.url), 'utf8'))
    .replaceAll('{{PROVISIONING_CSRF}}', 'fixture')
    .replaceAll('{{PROVISIONING_EXTENSION_ID}}', 'a'.repeat(32))
    .replaceAll('{{HOSTED_ONBOARDING_URL}}', name.includes('unavailable') ? '' : 'https://setup.example/')
    .replaceAll('{{HOSTED_ONBOARDING_VISIBILITY}}', name.includes('unavailable') ? 'hidden' : '');
  const source = (await readFile(new URL('../../app/provisioning/provisioning.js', import.meta.url), 'utf8'))
    .replace('    controller.start();', '    window.__provisioningController = controller; window.__provisioningStarted = controller.start();');
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/provisioning') return route.fulfill({ contentType: 'text/html', body: html });
    if (path === '/provisioning/provisioning.js') {
      if (deliveryDelay) await new Promise((resolve) => setTimeout(resolve, deliveryDelay));
      return route.fulfill({ contentType: 'text/javascript', body: source });
    }
    return route.abort();
  });
  await page.addInitScript(({ stage, name, acquireRefusal }) => {
    const pending = new Map();
    const fixture = window.__provisioningFixture = { stage, calls: [], hold: [], refusal: null, acquireRefusal,
      identity: 'fixture-account', unavailable: false, malformed: false, expired: false, identityUnavailable: false, portMissing: false, loseFinalize: false,
      release: (path) => { pending.get(path)?.(); pending.delete(path); },
      push: (stage) => fixture.onStage?.({ type: 'state', version: 1, stage, attempt: null }),
    };
    window.chrome = { runtime: {
      sendMessage: (_id, _message, done) => done(fixture.identityUnavailable ? null : { type: 'provisioning.identity.result', version: 1,
        authenticated_profile: fixture.identity ? { creator_account_id: fixture.identity } : null }),
      connect: () => { if (fixture.portMissing) throw new Error('Fixture port absent'); return { onMessage: { addListener: (fn) => { fixture.onStage = fn; setTimeout(() => fixture.push('ready_to_pair'), 0); } },
        onDisconnect: { addListener(fn) { fixture.disconnect = fn; } }, postMessage() {}, disconnect() {} }; },
    } };
    window.fetch = async (path) => {
      const operation = path.split('/').at(-1);
      fixture.calls.push(operation);
      if (fixture.hold.includes(operation)) await new Promise((resolve) => pending.set(operation, resolve));
      if (fixture.unavailable) throw new Error('Fixture offline');
      let payload;
      if (operation === 'status') payload = fixture.malformed ? {} : fixture.stage === null ? { state: 'configured_restart' } : {
        state: 'provisioning_ready', stage: fixture.stage,
        association_request_id: ['creator_approval_pending', 'finalization_ready'].includes(fixture.stage) ? 'fixture-association' : null,
        creator_account_id: ['creator_approval_pending', 'finalization_ready'].includes(fixture.stage) ? 'fixture-account' : null,
      };
      if (operation === 'claim') { fixture.stage = 'creator_confirmation_required'; payload = { state: 'installation_registered' }; }
      if (operation === 'creator-association') { fixture.stage = 'creator_approval_pending'; payload = { association_request_id: 'fixture-association', status: 'pending', updated_at: '2026-06-30T12:00:00Z' }; }
      if (operation === 'acquire') payload = { association_request_id: 'fixture-association', status: 'approved' };
      if (operation === 'finalize') { fixture.stage = null; if (fixture.loseFinalize) throw new Error('Fixture response lost'); payload = { state: 'configured_restart' }; }
      const refusal = fixture.refusal ?? (operation === 'acquire' ? fixture.acquireRefusal : null)
        ?? (['approve', 'approval-pending', 'approval-offline', 'approval-unavailable', 'approval-unavailable-help'].includes(name) && operation === 'acquire' ? 'binding_acquisition_unavailable' : null);
      return { ok: !refusal && !fixture.expired, status: fixture.expired ? 403 : refusal ? 409 : 200, json: async () => refusal ? { reason: refusal } : payload };
    };
    if (name === 'finish') fixture.hold.push('finalize');
  }, { stage, name, acquireRefusal });
}

export async function settleProvisioningFixture(page, { pendingOperation = null } = {}) {
  if (pendingOperation) {
    // Startup now automatically resumes approval/finalization. A deliberately
    // held response must be captured while it is pending, before start resolves.
    await page.waitForFunction((operation) => window.__provisioningFixture?.hold.includes(operation)
      && window.__provisioningFixture.calls.includes(operation), pendingOperation);
    await page.locator('.step[data-state="current"][aria-busy="true"]').waitFor({ state: 'visible' });
    return;
  }
  await page.evaluate(() => window.__provisioningStarted);
}

export async function openProvisioningFixture(page, options = {}) {
  await page.goto('http://provisioning-fixture.localhost/provisioning');
  await settleProvisioningFixture(page, options);
}
