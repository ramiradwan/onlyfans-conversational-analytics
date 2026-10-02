const settle = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
export async function driveWorkspaceDetails(page, view, step, unseen) {
  const push = (method, ...args) => page.evaluate(({ method, args }) => window.__workspaceFixture[method](...args), { method, args });
  const sample = await push('sample');
  const snapshot = (patch = {}) => push('snapshot', 'populated', { ...sample, ...patch });
  const refresh = async (key, patch, journey = 'populated') => {
    await push('refresh'); await settle(page);
    for (const pending of await push('pending')) await push('release', pending, journey, pending === key ? patch : undefined);
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

    for (const commercial_authority of ['required', 'active', 'unavailable']) for (const analysis_admission of ['admitted', 'blocked']) await step(`readiness:${commercial_authority}:${analysis_admission}`, () => refresh('activation.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission }));
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
  for (const desired_state of ['not_started', 'running', 'paused', 'revoked']) for (const effective_state of ['not_applied', desired_state]) await step(`history:${desired_state}:${effective_state}`, () => refresh('history.get', { ...history, desired_state, effective_state, consent_revision: desired_state === 'not_started' ? null : history.consent_revision }));
  for (const commercial_authority of ['required', 'active', 'unavailable']) for (const analysis_admission of ['admitted', 'blocked']) await step(`activation:${commercial_authority}:${analysis_admission}`, () => refresh('activation.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority, analysis_admission }));
  const vault = await push('apiSample', 'vault');
  for (const policy_type of ['disabled', 'finite', 'indefinite_until_delete']) for (const capable of [false, true]) for (const deletion of [null, 'pending', 'incomplete', 'complete']) await step(`vault:${policy_type}:${capable}:${deletion}`, () => refresh('vault.get', { ...vault,
    policy: { ...vault.policy, enabled: policy_type !== 'disabled', policy_type },
    capabilities: { ...vault.capabilities, export: capable, indefinite_retention: capable },
    deletion_operation: deletion ? { operation_id: 'fixture-operation', status: deletion, deletion_revision: 1 } : null,
  }));
  await refresh('vault.get', { ...vault, policy: { ...vault.policy, enabled: false, policy_type: 'disabled' } });
  await step('archive:open', () => page.getByRole('button', { name: 'Turn on archive', exact: true }).click());
  await step('archive:invalid', () => page.getByRole('spinbutton', { name: 'Days to keep' }).fill('0'));
  await step('archive:indefinite', () => page.getByLabel('Keep until I delete them').check());
  await step('archive:cancel', () => page.getByRole('button', { name: 'Cancel', exact: true }).click());
  await step('delete:open', () => page.getByRole('button', { name: 'Delete all messages', exact: true }).click());
  await step('delete:pending', () => page.getByRole('button', { name: 'Delete all', exact: true }).click());
  await step('delete:error', () => push('reject', 'vault.command', 'Error'));
  await refresh('activation.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' });
  await step('activation:open', () => page.getByRole('button', { name: 'Turn on full analytics', exact: true }).click());
  await step('activation:invalid', async () => { await page.getByRole('textbox', { name: 'Activation code' }).fill(unseen); await page.getByRole('button', { name: 'Activate', exact: true }).click(); });
  await step('activation:cancel', () => page.getByRole('button', { name: 'Cancel', exact: true }).click());
  for (const outcome of ['refusal', 'network', 'success']) {
    await refresh('activation.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: 'required', analysis_admission: 'blocked' });
    await step('redemption:' + outcome + ':open', () => page.getByRole('button', { name: 'Turn on full analytics', exact: true }).click());
    await page.getByRole('textbox', { name: 'Activation code' }).fill('clr1.' + 'a'.repeat(43));
    await step('redemption:' + outcome + ':pending', () => page.getByRole('button', { name: 'Activate', exact: true }).click());
    await step('redemption:' + outcome + ':response', () => outcome === 'success' ? push('resolve', 'activation.redeem', { state: 'checking' }) : push('reject', 'activation.redeem', 'Error'));
    await step('redemption:' + outcome + ':readiness', () => outcome === 'network' ? push('reject', 'activation.readiness', 'Error') : push('resolve', 'activation.readiness', { schema: 'ofca-analysis-readiness/v1', commercial_authority: outcome === 'success' ? 'active' : 'required', analysis_admission: 'admitted' }));
    if (outcome !== 'success') await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  }
  for (const outcome of ['failure', 'success']) {
    await refresh('vault.get', vault);
    await step('export:' + outcome + ':pending', () => page.getByRole('button', { name: 'Download messages', exact: true }).click());
    await step('export:' + outcome + ':response', () => outcome === 'failure' ? push('reject', 'vault.exportDocument', 'Error') : push('resolve', 'vault.exportDocument', { manifest: { copy_domains: { managed_recovery: { copies_may_remain: true } } }, conversations: [], messages: [] }));
    await step('deletion:' + outcome + ':open', () => page.getByRole('button', { name: 'Delete all messages', exact: true }).click());
    await step('deletion:' + outcome + ':pending', () => page.getByRole('button', { name: 'Delete all', exact: true }).click());
    await step('deletion:' + outcome + ':response', () => push('resolve', 'vault.command', { action: 'delete_all', status: { ...vault, deletion_operation: outcome === 'failure' ? { operation_id: 'fixture-operation', status: 'incomplete', deletion_revision: 1 } : null }, deletion_operation: { status: outcome === 'failure' ? 'incomplete' : 'complete' } }));
  }
  await refresh('pairing.pins', undefined, 'fresh');
  const pin = await push('apiSample', 'pairing');
  for (const stage of ['unavailable', 'needs_terms', 'paused', 'needs_full', 'needs_site_access', 'needs_account', 'ready_to_pair', 'pairing', 'paired']) await step(`port:${stage}`, () => push('pushPort', { status: 'connected', stage, attempt: null }));
  await push('pushPort', { status: 'absent', stage: null, attempt: null });
  await step('pairing:open', () => page.getByRole('button', { name: 'Connect extension', exact: true }).click());
  await step('pairing:pending', () => push('resolve', 'pairing.open', { ...pin, state: 'open', version: 0, comparison_code: null, agent_identity_thumbprint: null, expires_at: new Date(Date.now() + 300_000).toISOString() }));
  await step('pairing:299999', () => page.clock.fastForward(299999));
  await step('pairing:300000', () => page.clock.runFor(1));
  let noticeRevision = 100;
  for (const outcome of ['declined', 'cancelled', 'confirmed', 'refused']) {
    const browser = outcome === 'confirmed' || outcome === 'refused';
    await push('pushPort', { status: browser ? 'connected' : 'absent', stage: browser ? 'ready_to_pair' : null, attempt: null });
    await step(`pairing:${outcome}:start`, () => page.getByRole('button', { name: /Connect (?:another )?extension/, exact: true }).click());
    const expires_at = await page.evaluate(() => new Date(Date.now() + 300_000).toISOString());
    await push('resolve', 'pairing.open', { ...pin, expires_at, state: 'open', version: 0, comparison_code: null, agent_identity_thumbprint: null });
    await settle(page);
    await push('companion', { creator_account_id: pin.creator_account_id, revision: ++noticeRevision, changed_at: expires_at });
    await settle(page);
    await step(`pairing:${outcome}:comparison`, () => push('resolve', 'pairing.get', { ...pin, expires_at, state: 'awaiting_confirmation', version: 1, comparison_code: '482731' }));
    if (browser) {
      await step(`pairing:${outcome}:extension`, () => push('pushPort', { status: 'connected', stage: 'pairing', attempt: outcome === 'refused' ? { state: 'failed', comparison_code: null } : { state: 'compare', comparison_code: '482731' } }));
      await step(`pairing:${outcome}:result`, () => push('resolve', outcome === 'refused' ? 'pairing.change' : 'pairing.confirmVerified', { ...pin, expires_at, state: outcome === 'refused' ? 'cancelled' : 'confirmed', version: 2 }));
    } else {
      await step(`pairing:${outcome}:action`, () => page.getByRole('button', { name: outcome === 'declined' ? "Codes don't match" : 'Cancel', exact: true }).click());
      await step(`pairing:${outcome}:result`, () => push('resolve', 'pairing.change', { ...pin, expires_at, state: outcome, version: 2 }));
    }
    await push('release', 'pairing.pins', 'fresh');
  }
}
