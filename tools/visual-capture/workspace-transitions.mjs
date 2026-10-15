import { publishActivation } from './desktop-decision-fixtures.mjs';
import { waitForPairingResultPresentation } from './pairing-result-completion.mjs';

const settle = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));

export async function openPairingFallback(page) {
  await page.getByRole('button', { name: 'Connection options', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Compare connection codes', exact: true }).click();
  await page.clock.fastForward(250);
  await dialog.waitFor({ state: 'hidden' });
}

export async function waitForPairingOpenPresentation(page) {
  // A resolved API promise and animation frames do not imply that React has
  // committed its task and countdown. Keep that render inside the open action.
  await page.getByText('Waiting for the browser extension to connect.', { exact: true }).waitFor();
  await page.getByText(/^Connection window expires in /).waitFor();
}

export async function drivePairingExpiry(page, pin, push, step) {
  const initialExpiry = await page.evaluate(() => new Date(Date.now() + 299_000).toISOString());
  await step('pairing:pending', async () => {
    await push('resolve', 'pairing.open', { ...pin, state: 'open', version: 0, comparison_code: null, agent_identity_thumbprint: null, expires_at: initialExpiry });
    await waitForPairingOpenPresentation(page);
  });
  await step('pairing:before-expiry', async () => {
    // Keep the existing one-second boundary deterministic while native frame
    // observation continues. Real driver/render latency cannot consume it.
    await page.clock.pauseAt(new Date(Date.parse(initialExpiry) - 1000));
    await page.getByText('Waiting for the browser extension to connect.', { exact: true }).waitFor();
  });
  await step('pairing:expired', async () => {
    const remaining = await page.evaluate(expiry => Date.parse(expiry) - Date.now(), initialExpiry);
    await page.clock.fastForward(Math.max(0, remaining));
    await page.getByText('Time ran out before the connection finished. Try again.', { exact: true }).waitFor();
    await page.clock.resume();
  });
}

export async function driveWorkspaceDetails(page, view, step, unseen) {
  const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
  const sample = await push('sample');
  const snapshot = (patch = {}) => push('snapshot', 'populated', { ...sample, ...patch });
  const refresh = async (key, patch, journey = 'populated') => {
    await push('refresh'); await settle(page);
    for (const pending of await push('pending')) await push('release', pending, journey, pending === key ? patch : undefined);
    await page.clock.fastForward(450);
    await settle(page);
  };
  if (view === 'home' || view === 'inbox') {
    for (const field of ['display_name', 'latest_message.text', 'coverage.reason_code']) await step(`long:conversation:${field}`, async () => {
      const conversations = structuredClone(sample.conversations);
      const path = field.split('.');
      if (path.length === 1) conversations[0][field] = unseen;
      else conversations[0][path[0]][path[1]] = unseen;
      await snapshot({ conversations });
    });
    for (const field of ['coverage', 'projection', 'live_freshness', 'catchup_freshness']) await step(`long:${field}:reason`, () => snapshot({ [field]: { ...sample[field], reason: unseen } }));
    for (const value of [0, null, 9_999_999, Number.MAX_SAFE_INTEGER]) await step(`counts:${value ?? 'null'}`, () => snapshot({
      analytics: Object.fromEntries(Object.entries(sample.analytics).map(([key, metric]) => [key, { ...metric, value }])),
      coverage: { ...sample.coverage, complete_conversations: value ?? 0, discovered_conversations: value },
    }));
    for (const phase of ['not_started', 'discovering', 'backfilling', 'complete', 'blocked', 'paused']) await step(`coverage:${phase}`, () => snapshot({ coverage: { ...sample.coverage, phase, status: phase === 'complete' ? 'complete' : 'partial', reason: unseen } }));
    await snapshot();
  }
  if (view === 'home') {
    const agent = await push('agentSample');
    for (const status of ['connected', 'stale', 'disconnected', 'degraded']) await step('agent:' + status, () => push('agent', { ...agent, status, degraded_reason: unseen }));
    await step('agent:config-mismatch', () => push('agent', { ...agent, applied_config_revision: null }));
    await push('agent', agent);
    for (const state of ['idle', 'connecting', 'handshaking', 'connected']) await step('connection:' + state, () => push('connection', state));
    for (const code of ['unauthorized', 'unsupported_feature', 'unknown']) for (const fatal of [false, true]) {
      await step('protocol:' + code + ':' + fatal, () => push('protocolError', { code, message: unseen, fatal }));
      await step('protocol:recovered', () => push('reconnect'));
      await snapshot();
      await push('connection', 'connected');
    }
    await step('read-model:degraded', () => push('disconnected'));
    await push('connection', 'connected');
    await step('read-model:resyncing', () => push('resync'));
    await step('read-model:realtime', () => snapshot());

    for (const commercial_authority of ['required', 'active', 'unavailable']) for (const analysis_admission of ['admitted', 'blocked']) await step(`readiness:${commercial_authority}:${analysis_admission}`, () => refresh('onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission }));
    for (const control of ['Details', /^Status:/, 'Open navigation']) {
      const button = page.getByRole('button', { name: control, exact: typeof control === 'string' });
      if (!await button.isVisible()) continue;
      await step(`overlay:${String(control)}:open`, () => button.click());
      await step(`overlay:${String(control)}:close`, () => page.keyboard.press('Escape'));
    }
  }
  if (view === 'inbox') {
    const conversationId = sample.conversations[0].conversation_id;
    const message = await push('messageSample', conversationId);
    for (const prior of ['empty', 'populated']) for (const state of ['pending', 'empty', 'success', 'error', 'long', 'attachment', 'prepend']) {
      await push('beginMessagePage', conversationId, true); await settle(page);
      await push('messagePage', { ...message, items: prior === 'empty' ? [] : message.items }, 'replace');
      await settle(page);
      await step(`message:${prior}:${state}`, async () => {
        await push('beginMessagePage', conversationId, true); await settle(page);
        if (state === 'pending') await push('beginMessagePage', conversationId);
        else if (state === 'error') await push('failMessagePage', conversationId, unseen);
        else await push('messagePage', { ...message, items: state === 'empty' ? [] : message.items.map((item, index) => ({ ...item,
          message_id: state === 'prepend' ? `older-${index}` : item.message_id,
          text: state === 'long' ? unseen : state === 'attachment' ? null : item.text,
        })) }, state === 'prepend' ? 'prepend' : 'replace');
      });
    }
    const back = page.getByRole('button', { name: 'Back to conversations' });
    if (await back.isVisible()) await step('inbox:back', () => back.click());
  }
  if (view === 'analytics') {
    await push('analytics', 'model'); await settle(page);
    for (const name of ['Table', 'Chart']) await step(`tone:${name}`, () => page.getByRole('button', { name, exact: true }).click());
    await step('dates:open', () => page.getByRole('button', { name: 'Change dates' }).click());
    await step('dates:invalid', () => page.getByLabel('Start date').fill('2100-01-01'));
    await step('dates:close', () => page.keyboard.press('Escape'));
  }
  if (view !== 'settings') return;
  const history = await push('apiSample', 'history');
  const agent = await push('agentSample');
  await step.fixture('fixture:before-history', async () => {
    await snapshot({ coverage: { ...sample.coverage, status: 'partial', phase: 'backfilling', complete_conversations: 86 } });
    await settle(page);
  });
  let settingsRevision = history.settings_revision;
  for (const effective_state of ['not_applied', 'running']) await step(`history:acknowledgement:${effective_state}`, async () => {
    const settings_revision = ++settingsRevision;
    await push('agent', { ...agent, required_history_settings_revision: settings_revision, applied_history_settings_revision: settings_revision });
    await page.clock.runFor(1);
    await page.waitForFunction(() => window.__workspaceFixture.pending().includes('history.get'));
    await push('resolve', 'history.get', { ...history, settings_revision, effective_state });
    await page.locator('#message-history').getByText(effective_state === 'running' ? 'Adding messages' : 'Starting', { exact: true }).waitFor();
  });
  await step.fixture('fixture:after-history', async () => { await snapshot(); await settle(page); });
  // Admission changes use the current owner and API, not fixture replacement.
  await step.fixture('fixture:activation-admission-setup', async () => {
    await push('desktopSnapshot', {});
    await settle(page);
    await push('resolve', 'onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission: 'admitted' });
    await settle(page);
  });
  for (const analysis_admission of ['blocked', 'admitted']) await step(`activation:admission:${analysis_admission}`, async () => {
    await push('resolve', 'onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'active', analysis_admission });
    await page.locator('#full-analytics').getByText(analysis_admission === 'blocked' ? 'Needs attention' : 'On', { exact: true }).waitFor();
  });
  await step.fixture('fixture:after-admission', () => push('agent', agent));
  for (const desired_state of ['not_started', 'running', 'paused', 'revoked']) for (const effective_state of ['not_applied', desired_state]) await step(`fixture-api:history:${desired_state}:${effective_state}`, () => refresh('history.get', { ...history, desired_state, effective_state, consent_revision: desired_state === 'not_started' ? null : history.consent_revision }));
  for (const commercial_authority of ['required', 'active', 'unavailable']) for (const analysis_admission of ['admitted', 'blocked']) await step(`fixture-api:activation:${commercial_authority}:${analysis_admission}`, () => refresh('onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission }));
  const vault = await push('apiSample', 'vault');
  for (const policy_type of ['disabled', 'finite', 'indefinite_until_delete']) for (const capable of [false, true]) for (const deletion of [null, 'pending', 'incomplete', 'complete']) await step(`fixture-api:vault:${policy_type}:${capable}:${deletion}`, () => refresh('vault.get', { ...vault,
    policy: { ...vault.policy, enabled: policy_type !== 'disabled', policy_type },
    capabilities: { ...vault.capabilities, export: capable, indefinite_retention: capable },
    deletion_operation: deletion ? { operation_id: 'fixture-operation', status: deletion, deletion_revision: 1 } : null,
  }));
  await step.fixture('fixture:refresh:vault.get', () => refresh('vault.get', { ...vault, policy: { ...vault.policy, enabled: false, policy_type: 'disabled' } }));
  await step('archive:open', () => page.getByRole('button', { name: 'Turn on archive', exact: true }).click());
  await step('archive:invalid', () => page.getByRole('spinbutton', { name: 'Days to keep' }).fill('0'));
  await step('archive:indefinite', () => page.getByLabel('Keep until I delete them').check());
  await step('archive:cancel', () => page.getByRole('button', { name: 'Cancel', exact: true }).click());
  await step('delete:open', () => page.getByRole('button', { name: 'Delete all messages', exact: true }).click());
  await step('delete:pending', () => page.getByRole('button', { name: 'Delete all', exact: true }).click());
  await step('delete:error', () => push('reject', 'vault.command', 'Error'));
  await step.fixture('fixture:refresh:onboarding.readiness', () => refresh('onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' }));
  for (const state of ['selection', 'waiting', 'review']) await step('activation:' + state, () => publishActivation(page, state));
  await step('activation:uncertain', async () => {
    await push('desktopActionError', 'reply_lost');
    await page.getByRole('button', { name: 'Activate for free' }).click();
  });
  await step('activation:accepted', () => publishActivation(page, 'accepted'));
  await step('activation:unavailable', () => publishActivation(page, 'unavailable'));
  await step.fixture('fixture:reset-activation-task', async () => {
    await push('desktopActionError', null);
    await push('desktopSnapshot', { decision: null, decision_status: null, authentication: null, allowed_actions: ['activation.review'], pending_operations: [] });
  });
  for (const outcome of ['refusal', 'network', 'success']) {
    await step.fixture('fixture:refresh:onboarding.readiness', () => refresh('onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' }));
    const toggle = page.getByRole('button', { name: 'Use an activation code', exact: true });
    if (await toggle.getAttribute('aria-expanded') !== 'true') await step('redemption:' + outcome + ':open', () => toggle.click());
    await step.fixture('fixture:activation-code-input', () => page.getByRole('textbox', { name: 'Activation code' }).fill('clr1.' + 'a'.repeat(43)));
    await step('redemption:' + outcome + ':pending', () => page.getByRole('button', { name: 'Activate', exact: true }).click());
    await step('redemption:' + outcome + ':response', () => outcome === 'success' ? push('resolve', 'activation.redeem', { state: 'checking' }) : push('reject', 'activation.redeem', 'Error'));
    await step('redemption:' + outcome + ':readiness', () => outcome === 'network' ? push('reject', 'onboarding.readiness', 'Error') : push('resolve', 'onboarding.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: outcome === 'success' ? 'active' : 'required', analysis_admission: outcome === 'success' ? 'admitted' : 'blocked' }));
    if (outcome !== 'success') await step.fixture('fixture:activation-code-close', () => toggle.click());
  }
  for (const outcome of ['failure', 'success']) {
    await step.fixture('fixture:refresh:vault.get', () => refresh('vault.get', vault));
    await step('export:' + outcome + ':pending', () => page.getByRole('button', { name: 'Download messages', exact: true }).click());
    await step('export:' + outcome + ':response', () => outcome === 'failure' ? push('reject', 'vault.exportDocument', 'Error') : push('resolve', 'vault.exportDocument', { manifest: { copy_domains: { managed_recovery: { copies_may_remain: true } } }, conversations: [], messages: [] }));
    await step('deletion:' + outcome + ':open', () => page.getByRole('button', { name: 'Delete all messages', exact: true }).click());
    await step('deletion:' + outcome + ':pending', () => page.getByRole('button', { name: 'Delete all', exact: true }).click());
    await step('deletion:' + outcome + ':response', () => push('resolve', 'vault.command', { action: 'delete_all', status: { ...vault, deletion_operation: outcome === 'failure' ? { operation_id: 'fixture-operation', status: 'incomplete', deletion_revision: 1 } : null }, deletion_operation: { status: outcome === 'failure' ? 'incomplete' : 'complete' } }));
  }
  await step.fixture('fixture:refresh:pairing.pins', () => refresh('pairing.pins', undefined, 'fresh'));
  const pin = await push('apiSample', 'pairing');
  await step('pairing:open', () => openPairingFallback(page));
  await drivePairingExpiry(page, pin, push, step);
  let noticeRevision = 100;
  for (const outcome of ['declined', 'cancelled', 'confirmed', 'refused']) {
    // Independent outcomes start with a fresh settings component, while the
    // document and its complete geometry evidence remain intact.
    await step.fixture('fixture:pairing-outcome-reset', async () => {
      await push('resetSettings');
      await settle(page);
      for (const pending of await push('pending')) await push('release', pending, 'fresh');
      await settle(page);
    });
    await step(`pairing:${outcome}:start`, () => openPairingFallback(page));
    const expires_at = await page.evaluate(() => new Date(Date.now() + 300_000).toISOString());
    await step.fixture('fixture:pairing-open-result', async () => {
      await push('resolve', 'pairing.open', { ...pin, expires_at, state: 'open', version: 0, comparison_code: null, agent_identity_thumbprint: null });
      await waitForPairingOpenPresentation(page);
    });
    await step.fixture('fixture:pairing-companion-notice', async () => {
      await push('companion', { creator_account_id: pin.creator_account_id, revision: ++noticeRevision, changed_at: expires_at });
      await settle(page);
    });
    await step(`pairing:${outcome}:comparison`, () => push('resolve', 'pairing.get', { ...pin, expires_at, state: 'awaiting_confirmation', version: 1, comparison_code: '482731' }));
    if (outcome === 'confirmed' || outcome === 'refused') {
      await step.fixture('fixture:pairing-codes-match', () => page.getByRole('checkbox', { name: 'The codes match' }).check());
      await step(`pairing:${outcome}:confirmation`, () => page.getByRole('button', { name: 'Confirm connection', exact: true }).click());
      await step(`pairing:${outcome}:result`, async () => {
        if (outcome === 'refused') await push('reject', 'pairing.change', 'Error');
        else {
          await push('resolve', 'pairing.change', { ...pin, expires_at, state: 'confirmed', version: 2 });
          await waitForPairingResultPresentation(page, outcome);
        }
      });
    } else {
      await step(`pairing:${outcome}:action`, () => page.getByRole('button', { name: outcome === 'declined' ? "Codes don't match" : 'Cancel', exact: true }).click());
      await step(`pairing:${outcome}:result`, async () => {
        await push('resolve', 'pairing.change', { ...pin, expires_at, state: outcome, version: 2 });
        await waitForPairingResultPresentation(page, outcome);
      });
    }
    await step.fixture('fixture:pairing-pins-release', () => push('release', 'pairing.pins', 'fresh'));
  }
}
