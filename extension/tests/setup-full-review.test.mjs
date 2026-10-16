import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { payload } from '../test-fixtures/browser-task.mjs';
import { createFullReviewPersistence, fullReviewIntent, FULL_REVIEW_INTENT_KEY } from '../runtime/onboarding-full-intent.mjs';

const record = { journey_id: '11111111-1111-4111-8111-111111111111',
  draft_scope: { scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) },
  draft: { terms_checked: true, risk_checked: true, full_checked: false } };
const model = { status: { consent: { mode: 'preview' }, phase: 'preview' },
  legal: { configured: true, flow: { terms_event_id: 'terms', risk_event_id: 'risk' } },
  pairing: { state: 'unpaired' }, config: {}, desktopLinked: false };
const initiallyHidden = new Set([...readFileSync(new URL('../setup.html', import.meta.url), 'utf8').matchAll(/<[^>]+>/gu)]
  .filter(([tag]) => /\bclass="[^"]*\bhidden\b[^"]*"/u.test(tag))
  .map(([tag]) => /\bid="([^"]+)"/u.exec(tag)?.[1]).filter(Boolean));

// Execute the production entry point, including its ordering of initialization,
// workspace read, storage notifications and rendering. Only platform/UI ports
// are replaced; Full-review state and intent code stay in the production bundle.
const stubs = {
  'document-observer.mjs': 'export const OBSERVER_REOPEN_TYPE="observer";',
  'onboarding-entry.mjs': 'export const WORKSPACE_MESSAGE_TYPE="workspace";',
  'onboarding-workspace.mjs': 'export const WORKSPACE_STATE_KEY="workspace-state";',
  'legal-activation-controller.mjs': 'export const LEGAL_ACCEPT_TERMS_MESSAGE_TYPE="terms", LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE="risk", LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE="activate", LEGAL_CHOOSE_MODE_MESSAGE_TYPE="choose";',
  'surface-client.mjs': `export function createSurfaceClient(render) { return { model: fixture.model, async start() { render(this.model); } }; }
    export const send=fixture.send, openSurface=()=>{}, secureExternalUrl=()=>null;
    export class NoticeError extends Error {}`,
  'presentation.mjs': `export const customerJourney=()=>fixture.journey,
    desktopOwnsCapture=(model)=>(model.status?.consent.mode==='full'
      || (model.status?.consent.mode==='paused' && model.status?.consent.resume_mode==='full'))
      && model.pairing?.desktop_control===true,
    needsAgreement=()=>false, modeChoiceAvailable=()=>false;`,
  'dom.mjs': `export const element=fixture.element, show=fixture.show, text=(id,value)=>{element(id).textContent=value};
    export const renderLoading=()=>{},renderJourney=()=>{},renderReadiness=()=>{},renderLegalLinks=()=>{};
    export const createPageActions=()=>({busy:false,lock(){},bind(id,fn){fixture.actions[id]=fn}});`,
  'actions.mjs': 'export const chooseMode=async()=>{},transition=()=>{},restoreAccess=()=>{},openCreatorAccount=()=>{};',
  'handoff.mjs': 'export const createHandoffFinisher=()=>()=>{},returnToDesktop=()=>{};',
  'setup-transfer.mjs': 'export const SETUP_TRANSFER_MESSAGE="transfer",bindSetupTransfer=()=>null,renderReceivingContext=()=>{};',
};
const bundle = await build({ entryPoints: [fileURLToPath(new URL('../setup.js', import.meta.url))],
  bundle: true, write: false, format: 'iife', plugins: [{ name: 'setup-platform-ports', setup(bundler) {
    bundler.onResolve({ filter: /\.mjs$/ }, ({ path }) => {
      const name = path.split('/').at(-1);
      return stubs[name] ? { path: name, namespace: 'fixture' } : undefined;
    });
    bundler.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({ contents: stubs[path], loader: 'js' }));
  } }] });
const settle = () => new Promise((resolve) => setImmediate(resolve));

function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key) };
}
async function initialize(saved, admitted = record, intent = null, options = {}) {
  const nodes = new Map(), actions = {}, visibility = [], calls = [];
  const element = (id) => {
    if (!nodes.has(id)) nodes.set(id, { hidden: initiallyHidden.has(id), dataset: {}, classList: { toggle() {} },
      setAttribute() {}, removeAttribute() {}, addEventListener() {}, insertBefore() {} });
    return nodes.get(id);
  };
  let admit;
  const admittedRead = new Promise((resolve) => { admit = resolve; });
  const listeners = [];
  const state = { intent };
  const fixture = { model: structuredClone(options.model ?? model), element, actions,
    journey: options.journey ?? { id: 'preview_available', title: 'Preview is ready' },
    show(id, visible) { element(id).hidden = !visible; visibility.push([id, visible]); },
    async send(message) {
      calls.push(message);
      if (message.action === 'browser_task') return options.task ?? null;
      if (message.action === 'desktop_adoption') return { adopted: options.adopted === true };
      if (message.action === 'draft' || message.action === 'dismiss_browser_task') return {};
      return message.type === 'workspace' ? admittedRead : null;
    } };
  runInNewContext(bundle.outputFiles[0].text, { fixture, sessionStorage: saved,
    location: { hash: `#journey=${admitted.journey_id}` },
    document: { querySelector: element, querySelectorAll: () => [] }, window: { addEventListener() {} },
    chrome: { storage: { onChanged: { addListener: (listener) => listeners.push(listener) },
      session: { get: async () => ({ [FULL_REVIEW_INTENT_KEY]: state.intent }) } } },
    setTimeout: (callback, ms) => { const timer = setTimeout(callback, ms); timer.unref(); return timer; }, clearTimeout, console });
  await settle();
  assert.notEqual(element('main').dataset.presentationReady, 'true', 'the task remains unrevealed before worker admission');
  assert.equal(element('full-disclosure').hidden, true, 'nothing is restored before worker admission');
  admit(structuredClone(admitted));
  await settle(); await settle();
  return { fixture, calls, element, visibility, async review(value) {
    state.intent = value;
    for (const listener of listeners) listener({ [FULL_REVIEW_INTENT_KEY]: { newValue: value } }, 'session');
    await settle(); await settle();
  } };
}


for (const id of ['pairing_required', 'activation_required', 'full_ready', 'desktop_app_needed']) {
  test(`Full ${id} has one desktop handoff instead of a competing setup workflow`, async () => {
    const full = structuredClone(model); full.status.consent.mode = 'full';
    const run = await initialize(storage(), record, null, { model: full, journey: { id, title: 'Full' } });
    assert.equal(run.element('desktop-entry').hidden, false);
    for (const section of ['pre-mode', 'mode-choice', 'access-card', 'journey-card']) assert.equal(run.element(section).hidden, true);
    assert.ok(run.calls.every(call => ['read', 'browser_task', 'desktop_adoption', 'context'].includes(call.action)));
  });
}

test('Preview-to-Full intent survives reload and keeps Preview running until an admitted task requests consent', async () => {
  const saved = storage(); createFullReviewPersistence(saved).write(record, true);
  const first = await initialize(saved);
  assert.equal(first.element('desktop-entry').hidden, false);
  assert.equal(first.element('full-disclosure').hidden, true);
  assert.equal(first.fixture.model.status.consent.mode, 'preview');
  const second = await initialize(saved);
  assert.equal(second.element('desktop-entry').hidden, false);
  await second.fixture.actions['desktop-back']();
  const third = await initialize(saved);
  assert.equal(third.element('desktop-entry').hidden, true);
  assert.equal(third.element('preview-metrics').hidden, false);
});

test('a saved workspace Full choice survives loss of tab storage after browser restart', async () => {
  const resumed = structuredClone(record); resumed.draft.full_checked = true;
  const run = await initialize(storage(), resumed);
  assert.equal(run.element('desktop-entry').hidden, false);
  assert.equal(run.fixture.model.status.consent.mode, 'preview');
});

test('only the admitted task opens Full consent; declining it preserves Preview', async () => {
  const task = { task: { ...payload().task, journey_id: record.journey_id }, draft_scope: record.draft_scope };
  const run = await initialize(storage(), record, null, { task });
  assert.equal(run.element('desktop-entry').hidden, true);
  assert.equal(run.element('full-disclosure').hidden, false);
  assert.equal(run.fixture.model.status.consent.mode, 'preview');
  await run.fixture.actions['full-secondary']();
  assert.ok(run.calls.some(call => call.action === 'dismiss_browser_task'));
  assert.equal(run.element('full-disclosure').hidden, true);
  assert.equal(run.element('preview-metrics').hidden, false);
});

for (const mismatch of ['journey', 'creator', 'disclosure']) {
  test(`a different ${mismatch} cannot restore Full intent`, async () => {
    const saved = storage(), prior = structuredClone(record);
    if (mismatch === 'journey') prior.journey_id = crypto.randomUUID();
    if (mismatch === 'creator') prior.draft_scope.scope_id = crypto.randomUUID();
    if (mismatch === 'disclosure') prior.draft_scope.disclosure_bundle_id = 'b'.repeat(64);
    createFullReviewPersistence(saved).write(prior, true);
    const run = await initialize(saved, record, fullReviewIntent(prior.journey_id, prior.draft_scope));
    assert.equal(run.element('desktop-entry').hidden, true);
    assert.equal(run.element('preview-metrics').hidden, false);
    assert.ok(!run.visibility.some(([id, visible]) => id === 'full-disclosure' && visible));
  });
}
