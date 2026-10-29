import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { lstat, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { chromium, expect } from '@playwright/test';

import { BrainProcess, BRAIN_ORIGIN, BRAIN_LOOPBACK_URL } from './brain.mjs';
import { completeBrowserWebAuthnCeremony } from './brain-probe.mjs';
import { assertBuiltSpa, PRODUCT_ROOT } from './paths.mjs';

export const fixture = JSON.parse(readFileSync(new URL('../fixtures/text-analysis.json', import.meta.url), 'utf8'));
const kinds = ['joint', 'split-partial', 'empty', 'saved-only'];
const ownerKeys = ['canonical', 'source', 'authentication', 'dependencies', 'execution', 'preview', 'client'];
const prefix = 'ofca-text-analysis-';
const samePath = (left, right) => process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right;
const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000;
const keys = (value, expected) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...expected].sort().join(',');
const digest = (value) => createHash('sha256').update(canonical(value)).digest('hex');
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
export function check(value, code) {
  if (!value) throw new Error(`text_analysis_${code}`);
}
export const sameMeaning = (left, right) => canonical(left) === canonical(right);
async function validateRoot(root, parent) {
  const resolved = path.resolve(root), metadata = await lstat(root);
  check(path.isAbsolute(root) && samePath(root, resolved) && metadata.isDirectory() && !metadata.isSymbolicLink(), 'root_invalid');
  check(samePath(path.dirname(resolved), parent) && path.basename(resolved).startsWith(prefix), 'root_invalid');
  check(samePath(await realpath(resolved), resolved), 'root_aliased');
  const relative = path.relative(PRODUCT_ROOT, resolved);
  check(relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative), 'root_inside_source');
}
export async function readJson(page, pathname) {
  check(pathname.startsWith('/api/v1/') || pathname === '/__e2e/text-analysis', 'read_path');
  return page.evaluate(async (url) => {
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    const text = await response.text();
    if (text.length > 2 * 1024 * 1024) throw new Error('text_analysis_response_limit');
    if (!response.ok) {
      let code = null;
      try {
        const value = JSON.parse(text)?.detail?.code;
        if (typeof value === 'string' && /^[a-z_]{1,80}$/.test(value)) code = value;
      } catch { /* An unsuccessful response contains no usable data. */ }
      return { status: response.status, value: null, code };
    }
    try { return { status: response.status, value: JSON.parse(text) }; }
    catch { throw new Error('text_analysis_response_invalid'); }
  }, pathname);
}
export async function stats(page) {
  const result = await readJson(page, '/__e2e/text-analysis');
  check(result.status === 200, 'stats_unavailable');
  const value = result.value;
  check(keys(value, ['schema', 'scenario', 'account_id', 'profile_id', 'profile_sha256', 'source_count', 'engine', 'canonical', 'owners'])
    && value.schema === 'e2e-text-analysis.v1' && kinds.includes(value.scenario)
    && value.account_id === fixture.account_id && typeof value.profile_id === 'string'
    && /^[a-f0-9]{64}$/.test(value.profile_sha256) && count(value.source_count), 'stats_invalid');
  check(keys(value.engine, ['prepare_count', 'analyze_count']) && keys(value.owners, ownerKeys)
    && keys(value.canonical, ['committed_jobs', 'members', 'preview_outputs', 'preview_spent', 'client_intents'])
    && ['prepare_count', 'analyze_count'].every((key) => count(value.engine?.[key]))
    && ['committed_jobs', 'members', 'preview_outputs', 'preview_spent', 'client_intents'].every((key) => count(value.canonical?.[key]))
    && ownerKeys.every((key) => value.owners?.[key] === true), 'stats_invalid');
  return value;
}
function observe(page) {
  let sequence = 0;
  const requests = [], acknowledgements = [], sessions = [], projections = [], pending = new Set(), errors = [];
  const closedConnections = new Set();
  page.on('pageerror', () => errors.push('page_error'));
  page.on('websocket', (socket) => {
    if (socket.url() !== BRAIN_ORIGIN.replace('http:', 'ws:') + '/ws/bridge') { errors.push('unexpected_websocket'); return; }
    let bound = null;
    socket.on('close', () => { if (bound !== null) closedConnections.add(bound.connection_id); });
    socket.on('framereceived', ({ payload }) => {
      try {
        const message = JSON.parse(String(payload));
        if (message.type === 'bridge.session') {
          bound = { connection_id: message.payload.connection_id,
            bridge_session_id: message.payload.bridge_session_id, creator_account_id: message.payload.creator_account_id };
          sessions.push(bound);
        } else if (message.type === 'state.snapshot' || message.type === 'state.delta') {
          const body = message.payload;
          check(bound !== null && body.creator_account_id === bound.creator_account_id && count(body.view_revision), 'projection_binding');
          const prior = projections.filter((item) => item.connection_id === bound.connection_id).at(-1);
          const projection = message.type === 'state.snapshot' ? body.projection
            : body.changes.find((item) => item.type === 'projection.replace')?.projection;
          if (projection) {
            check(keys(projection, ['status', 'canonical_revision', 'projected_revision', 'projected_at', 'reason'])
              && ['pending', 'current', 'degraded', 'unavailable'].includes(projection.status)
              && count(projection.canonical_revision) && count(projection.projected_revision)
              && [null, 'projection_missing', 'projection_activation_pending', 'projection_degraded', 'projection_lag'].includes(projection.reason), 'projection_invalid');
            const conversations = message.type === 'state.snapshot' ? body.conversations.length : prior?.conversation_count;
            check(count(conversations) && projections.length < 128, 'projection_observation_limit');
            projections.push({ ...bound, view_revision: body.view_revision, conversation_count: conversations,
              status: projection.status, canonical_revision: projection.canonical_revision,
              projected_revision: projection.projected_revision, reason: projection.reason });
          }
        }
      } catch { errors.push('websocket_frame'); }
    });
  });
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin === BRAIN_ORIGIN && request.method() === 'POST'
      && url.pathname.startsWith('/api/v1/insights/questions')
      && !['/api/v1/insights/questions/evidence', '/api/v1/insights/questions/evidence/context'].includes(url.pathname)) errors.push('question_warmup');
    if (url.origin !== BRAIN_ORIGIN || request.method() !== 'POST' || !url.pathname.startsWith('/api/v1/nlp/')) return;
    try { requests.push({ sequence: sequence++, path: url.pathname, body: request.postDataJSON(), csrf: Boolean(request.headers()['x-csrf-token']) }); }
    catch { errors.push('request_body'); }
  });
  page.on('response', (response) => {
    const request = response.request(), url = new URL(response.url());
    if (url.origin !== BRAIN_ORIGIN || url.pathname !== '/api/v1/nlp/client-intents' || request.method() !== 'POST' || response.status() !== 200) return;
    const seen = sequence++;
    const work = (async () => {
      try {
        const value = await response.json(), sent = request.postDataJSON();
        check(value.schema_version === 'text-analysis-client-intent-record.v1' && value.kind === sent.kind && value.key === sent.key
          && canonical(value.body) === canonical(sent.body) && value.body_sha256 === digest(sent.body), 'acknowledgement_invalid');
        acknowledgements.push({ sequence: seen, value });
      } catch { errors.push('acknowledgement'); }
    })();
    pending.add(work); work.finally(() => pending.delete(work));
  });
  return { requests, acknowledgements, sessions, projections, closedConnections, errors, async settle() { await Promise.all([...pending]); } };
}
async function bridgeCurrent(log, bound, conversationCount = fixture.messages.length) {
  await expect.poll(() => {
    const current = log.projections.filter((item) => item.connection_id === bound.connection_id).at(-1);
    return Boolean(current && log.sessions.at(-1)?.connection_id === bound.connection_id && !log.closedConnections.has(bound.connection_id)
      && current.bridge_session_id === bound.bridge_session_id && current.creator_account_id === fixture.account_id
      && current.status === 'current' && current.reason === null && current.canonical_revision === 1
      && current.projected_revision === current.canonical_revision && current.view_revision > 0
      && current.conversation_count === conversationCount);
  }, { message: 'The authenticated Bridge conversation projection must be current.' }).toBe(true);
}
export function assertJournalOrder(log) {
  const commands = log.requests.filter((item) => item.path === '/api/v1/nlp/execution' || item.path === '/api/v1/nlp/previews'
    || /^\/api\/v1\/nlp\/previews\/[^/]+\/confirm$/.test(item.path));
  check(commands.length > 0, 'domain_request_missing');
  for (const command of commands) {
    const kind = command.path === '/api/v1/nlp/execution' ? 'enrollment_command'
      : command.path === '/api/v1/nlp/previews' ? 'preview_prepare' : 'preview_confirm';
    const key = command.body.command_key ?? command.body.external_key;
    const ack = log.acknowledgements.find((item) => item.sequence < command.sequence && item.value.kind === kind && item.value.key === key);
    check(Boolean(ack) && canonical(kind === 'preview_confirm' ? ack.value.body.confirmation : ack.value.body) === canonical(command.body), 'journal_order');
    check(command.csrf, 'csrf_missing');
  }
}
export async function dropNextReply(page, pathname) {
  check(pathname === '/api/v1/nlp/execution' || pathname === '/api/v1/nlp/previews'
    || /^\/api\/v1\/nlp\/previews\/[a-f0-9-]{36}\/confirm$/.test(pathname), 'drop_path');
  let resolve, reject, entered = false;
  const received = new Promise((done, fail) => { resolve = done; reject = fail; });
  const timer = setTimeout(() => reject(new Error('text_analysis_reply_observation_timeout')), 15_000);
  const closed = () => { clearTimeout(timer); reject(new Error('text_analysis_reply_observation_closed')); };
  page.once('close', closed);
  void received.catch(() => undefined);
  const handler = async (route) => {
    if (route.request().method() !== 'POST' || entered) { await route.continue(); return; }
    entered = true;
    let status = null;
    try {
      const response = await route.fetch({ url: BRAIN_LOOPBACK_URL + pathname,
        headers: { ...await route.request().allHeaders(), host: new URL(BRAIN_ORIGIN).host },
        maxRedirects: 0, timeout: 12_000 });
      status = response.status();
      check(status === 200, 'forwarded_request_failed');
      const value = await response.json(), body = route.request().postDataJSON();
      await response.dispose();
      await route.abort('failed'); clearTimeout(timer); resolve({ body, value });
    } catch {
      try { await route.abort('failed'); } catch { /* The original request may already be closed. */ }
      clearTimeout(timer); reject(new Error(`text_analysis_reply_observation_failed_${status ?? 'transport'}`));
    }
  };
  await page.route(BRAIN_ORIGIN + pathname, handler);
  return { received, async close() { clearTimeout(timer); page.off('close', closed); await page.unroute(BRAIN_ORIGIN + pathname, handler); } };
}
export async function ready(page, log, route = '/settings', conversationCount = fixture.messages.length) {
  const sessions = log.sessions.length;
  await page.goto(BRAIN_ORIGIN + route, { waitUntil: 'domcontentloaded' });
  await expect.poll(() => log.sessions.length > sessions, { message: 'A new authenticated Bridge connection is required.' }).toBe(true);
  const bound = log.sessions.at(-1);
  check(bound.creator_account_id === fixture.account_id && typeof bound.connection_id === 'string' && typeof bound.bridge_session_id === 'string', 'bridge_binding');
  check(await page.evaluate((account) => {
    const config = JSON.parse(document.getElementById('fastapi-config')?.textContent ?? '{}');
    return config.CREATOR_ID === account && config.BRIDGE_ROLE === 'creator' && typeof config.BRIDGE_AUTH_TICKET === 'string'
      && config.BRIDGE_AUTH_TICKET.length > 0 && Boolean(document.querySelector('meta[name="csrf-token"]')?.content)
      && !('__TAURI__' in globalThis);
  }, fixture.account_id), 'bootstrap_binding');
  await stats(page);
  await bridgeCurrent(log, bound, conversationCount);
  if (route === '/analytics') await page.getByRole('tab', { name: 'Text analysis', exact: true }).click();
}
export async function prepareProfile(x) {
  await ready(x.page, x.log, '/settings', x.conversationCount);
  const before = await stats(x.page);
  check(before.engine.prepare_count === 0 && before.engine.analyze_count === 0, 'startup_analysis');
  const button = x.page.getByRole('button', { name: `Prepare ${before.profile_id}`, exact: true });
  await expect(button).toBeEnabled(); await button.click();
  await expect.poll(async () => (await stats(x.page)).engine.prepare_count).toBeGreaterThan(0);
  await expect(button).toBeDisabled();
  check((await stats(x.page)).engine.analyze_count === 0, 'prepare_analysis');
}
export async function selectProfile(page, label, scenario) {
  await page.getByRole('combobox', { name: label, exact: true }).click();
  const profile = fixture.scenarios[scenario];
  await page.getByRole('option', { name: `${profile.profile_id} · ${profile.revision}`, exact: true }).click();
}
export async function processingCommand(page, action, { unknown = false } = {}) {
  const panel = page.getByRole('region', { name: 'Account text processing', exact: true });
  await expect(panel.locator('[data-journey-state="textanalysis.processing_controls"]')).toBeVisible();
  const labels = { enable: 'Review enablement', pause: 'Pause processing', resume: 'Resume processing', retire: 'Retire processing' };
  await panel.getByRole('button', { name: labels[action], exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Review processing change', exact: true });
  await expect(dialog.locator('[data-journey-state="textanalysis.processing_review"]')).toBeVisible();
  await dialog.getByRole('button', { name: 'Confirm processing change', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  if (unknown) return;
  await expect(panel.getByText(`Processing: ${action === 'pause' ? 'paused' : action === 'retire' ? 'not enrolled' : 'enabled'}.`, { exact: true })).toBeVisible();
}
export async function preparePreview(x, scenario = x.scenario) {
  await ready(x.page, x.log, '/analytics');
  await selectProfile(x.page, 'Approved preview profile', scenario);
  const panel = x.page.getByRole('region', { name: 'Text analysis previews', exact: true });
  await expect(panel.locator('[data-journey-state="textanalysis.preview_controls"]')).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Prepare preview scope', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Prepare preview scope', exact: true }).click();
  await expect(panel.locator('[data-journey-state="textanalysis.preview_scope"]')).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Confirm original preview', exact: true })).toBeEnabled();
  await x.log.settle();
  const command = x.log.requests.filter((item) => item.path === '/api/v1/nlp/previews').at(-1);
  check(Boolean(command), 'prepare_missing');
  const result = await readJson(x.page, `/api/v1/nlp/previews/${command.body.external_key}`);
  check(result.status === 200 && result.value.receipt.state === 'prepared' && result.value.confirmation
    && result.value.disclosure.languages.join(',') === 'en' && result.value.scope.source_count === fixture.messages.length
    && result.value.scope.message_cap === 32 && result.value.scope.inventory_sha256 === result.value.confirmation.input_inventory_sha256
    && digest(result.value.scope) === result.value.confirmation.scope_sha256, 'prepared_scope');
  await expect(panel.getByText(`Frozen sample: ${fixture.messages.length} messages, up to 32, received and sent.`, { exact: true })).toBeVisible();
  const before = await stats(x.page);
  check(before.engine.analyze_count === 0 && before.canonical.preview_spent === 0 && before.canonical.preview_outputs === 0, 'prepare_spend');
  return result.value;
}
export async function confirmPreview(x, original) {
  await x.page.getByRole('button', { name: 'Confirm original preview', exact: true }).click();
  await expect.poll(async () => (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value?.receipt.state,
    { message: 'The original preview must reach its saved terminal state.' }).toBe('committed');
  const outcome = (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value;
  check(outcome.receipt.job_id === original.receipt.job_id && outcome.receipt.contribution_id === original.receipt.contribution_id, 'preview_identity');
  await x.page.getByRole('button', { name: 'Check retained previews', exact: true }).click();
  await openSaved(x.page);
  return saved(x.page, original.receipt.contribution_id);
}
export async function openSaved(page) {
  const open = page.getByRole('button', { name: 'Open saved preview 1', exact: true });
  await expect(open).toBeEnabled(); await open.click();
  await expect(page.getByLabel('Verified shown preview', { exact: true })).toBeVisible();
  await expect(page.locator('[data-journey-state="textanalysis.preview_results"]')).toBeVisible();
}
export async function saved(page, id) {
  const result = await readJson(page, `/api/v1/nlp/previews/results/${id}/view`);
  check(result.status === 200 && result.value.output.contribution_id === id, 'saved_missing');
  return result.value;
}
export async function assertSummary(page, envelope, scenario) {
  const expected = fixture.scenarios[scenario], output = envelope.output;
  check(output.summaries.length === 2 && output.summaries.every((item) => ['topic_category', 'mention_category'].includes(item.kind)), 'summary_kinds');
  for (const [kind, name] of [['topic_category', 'topic'], ['mention_category', 'mentions']]) {
    const item = output.summaries.find((row) => row.kind === kind), wanted = expected[name];
    check(item && ['category_id', 'display_label', 'label_status'].every((key) => item.label[key] === wanted[key])
      && ['supporting_conversations', 'supporting_messages', 'occurrences', 'ranking_scope', 'additional_categories'].every((key) => item[key] === wanted[key]), 'summary_values');
    const section = page.getByRole('region', { name: kind === 'mention_category' ? 'Preview mention winner' : 'Preview topic winner', exact: true });
    await expect(section.getByText(`${kind === 'mention_category' ? 'Exact mentions' : 'Topics'}: ${wanted.display_label}`, { exact: true })).toBeVisible();
    await expect(section.getByText(`${wanted.supporting_conversations} supporting conversations · ${wanted.supporting_messages} distinct messages${kind === 'mention_category' ? ` · ${wanted.occurrences} exact occurrences` : ''}.`, { exact: true })).toBeVisible();
    if (wanted.additional_categories === null) {
      await expect(section.getByText(/additional .* categories in the fully assessed frozen scope/)).toHaveCount(0);
      await expect(section.getByText('Ranked by observed support in the assessed portion; the full ranking is unavailable.', { exact: true })).toBeVisible();
    } else {
      await expect(section.getByText(`+${wanted.additional_categories} additional ${kind === 'mention_category' ? 'mention' : 'topic'} categories in the fully assessed frozen scope.`, { exact: true })).toBeVisible();
      await expect(section.getByText('Ranked across the entire frozen scope.', { exact: true })).toBeVisible();
    }
  }
}
export async function assertOccurrences(page, envelope) {
  const item = envelope.output.summaries.find((row) => row.kind === 'mention_category');
  check(item?.evidence.length === 4, 'occurrence_count');
  const region = page.getByRole('region', { name: 'Preview mention winner', exact: true });
  const seen = new Set();
  for (let index = 0; index < item.evidence.length; index += 1) {
    const evidence = item.evidence[index];
    const [request] = await Promise.all([
      page.waitForRequest((entry) => entry.method() === 'POST'
        && entry.url() === `${BRAIN_ORIGIN}/api/v1/nlp/previews/results/${envelope.output.contribution_id}/evidence`, { timeout: 12_000 }),
      region.getByRole('button', { name: new RegExp(`^Open occurrence evidence ${index + 1} ·`) }).click(),
    ]);
    check(sameMeaning(request.postDataJSON(), evidence.reference) && Boolean(request.headers()['x-csrf-token']), 'shown_evidence_request');
    const dialog = page.getByRole('dialog', { name: 'Finding source', exact: true });
    await expect(page.locator('[data-journey-state="findings.source"]').filter({ has: dialog })).toBeVisible();
    await expect(dialog.locator('blockquote')).toBeVisible();
    const text = await dialog.locator('blockquote').textContent();
    const message = fixture.messages.find((entry) => entry.text === text);
    check(Boolean(message), 'source_message');
    const expected = message.mentions.find((span) => span.start === evidence.reference.span.start && span.end === evidence.reference.span.end);
    check(Boolean(expected), 'occurrence_span');
    const key = `${message.message_id}:${expected.start}:${expected.end}`;
    check(!seen.has(key), 'duplicate_occurrence'); seen.add(key);
    await expect(dialog.locator('blockquote')).toHaveText(message.text);
    await expect(dialog.locator('mark')).toHaveText(expected.text);
    const exact = await dialog.locator('mark').evaluate((mark) => ({ before: mark.previousSibling?.textContent ?? '', after: mark.nextSibling?.textContent ?? '' }));
    const points = Array.from(message.text);
    check(exact.before === points.slice(0, expected.start).join('') && exact.after === points.slice(expected.end).join(''), 'unicode_highlight');
    await expect(dialog.getByText(new RegExp(`^${message.direction === 'inbound' ? 'Received' : 'Sent'} ·`))).toBeVisible();
    await dialog.getByRole('button', { name: 'Close', exact: true }).click(); await expect(dialog).toHaveCount(0);
  }
  check(seen.size === fixture.messages.reduce((total, message) => total + message.mentions.length, 0), 'occurrence_inventory');
}
export async function withTextAnalysis(scenario, run, { sharedConversation = false } = {}) {
  check(['joint', 'split-partial', 'empty'].includes(scenario), 'scenario_invalid'); assertBuiltSpa();
  check(fixture.schema === 'e2e-text-analysis-source.v1', 'fixture_invalid');
  const conversationCount = new Set(fixture.messages.map((message) => sharedConversation
    ? fixture.messages[0].conversation_id : message.conversation_id)).size;
  const parent = await realpath(tmpdir()), root = await mkdtemp(path.join(parent, prefix));
  await validateRoot(root, parent);
  let browser, context, brain, failure;
  const errors = [];
  try {
    const paths = { authDatabasePath: path.join(root, 'auth.sqlite3'), canonicalDatabasePath: path.join(root, 'canonical.sqlite3'),
      projectionDatabasePath: path.join(root, 'projections.sqlite3'), extensionId: 'a'.repeat(32),
      environmentOverrides: { OFCA_E2E_TEXT_ANALYSIS_SHARED_CONVERSATION: sharedConversation ? '1' : '0' } };
    brain = new BrainProcess({ ...paths, textAnalysisFixture: scenario }); await brain.start();
    browser = await chromium.launch({ headless: true, ...(process.env.OFCA_E2E_BROWSER_EXECUTABLE ? { executablePath: process.env.OFCA_E2E_BROWSER_EXECUTABLE } : {}),
      args: ['--host-resolver-rules=MAP bridge.localhost 127.0.0.1', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run'] });
    context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.route('**/*', (route) => new URL(route.request().url()).origin === BRAIN_ORIGIN ? route.continue() : route.abort('blockedbyclient'));
    const page = await context.newPage(), log = observe(page);
    await completeBrowserWebAuthnCeremony(page);
    await ready(page, log, '/settings', conversationCount);
    const initial = await stats(page);
    check(initial.scenario === scenario && initial.source_count === fixture.messages.length
      && initial.engine.prepare_count === 0 && initial.engine.analyze_count === 0
      && Object.values(initial.canonical).every((value) => value === 0), 'initial_state');
    const x = { page, log, scenario, root, conversationCount, async restart(next = scenario) {
      check(kinds.includes(next), 'scenario_invalid');
      const prior = log.sessions.at(-1), sessionCount = log.sessions.length;
      await brain.stop();
      brain = new BrainProcess({ ...paths, textAnalysisFixture: next }); await brain.start();
      await expect.poll(() => log.sessions.length > sessionCount, { message: 'The real Bridge connection must recover.' }).toBe(true);
      const current = log.sessions.at(-1);
      check(current.creator_account_id === prior.creator_account_id && current.bridge_session_id === prior.bridge_session_id
        && current.connection_id !== prior.connection_id, 'reconnection_binding');
      await bridgeCurrent(log, current, conversationCount);
      check((await stats(page)).scenario === next, 'restart_scenario');
    } };
    await run(x);
    await log.settle(); check(log.errors.length === 0, 'browser_errors');
  } catch (error) { failure = error; }
  try { await context?.close(); } catch (error) { errors.push(error); }
  try { await browser?.close(); } catch (error) { errors.push(error); }
  try { await brain?.stop(); } catch (error) { errors.push(error); }
  if (!failure && errors.length === 0) {
    try { await validateRoot(root, parent); await rm(root, { recursive: true, force: false }); } catch (error) { errors.push(error); }
  }
  if (failure || errors.length) throw new AggregateError([...(failure ? [failure] : []), ...errors], 'text_analysis_browser_failed');
}
