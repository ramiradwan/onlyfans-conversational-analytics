import assert from 'node:assert/strict';
import test from 'node:test';
import { bindSetupTransfer, submitReceivingEntry, renderReceivingContext } from '../ui/setup-transfer.mjs';

const hostedOrigin = 'https://setup.example.com';
const payload = { journey_id: '11111111-1111-4111-8111-111111111111',
  hosted_start_url: `${hostedOrigin}/public/onboarding/setup/receive`,
  request: { profile: 'urn:bridge-clean:onboarding-transfer:v1', setup_code: '0123456789AB',
    destination: { kind: 'browser-extension' } } };
function page() {
  const nodes = new Map(), forms = [];
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, { id, handlers: {}, value: '', classList: {
      values: new Set(), remove(name) { this.values.delete(name); },
      toggle(name, enabled) { if (enabled) this.values.add(name); else this.values.delete(name); },
    },
      addEventListener(name, fn) { this.handlers[name] = fn; }, setAttribute() {}, removeAttribute() {}, focus() { this.focused = true; } });
    return nodes.get(id);
  };
  const document = { getElementById: node, querySelector: node, body: { append(form) { forms.push(form); } },
    createElement(tag) { return { tag, children: [], append(child) { this.children.push(child); }, submit() { this.submitted = true; } }; } };
  return { document, node, forms };
}
test('receiving entry uses the same tab and a body, never a code or key in a URL', () => {
  const run = page(); submitReceivingEntry(run.document, payload, hostedOrigin);
  const form = run.forms[0];
  assert.equal(form.method, 'post'); assert.equal(form.target, '_self');
  assert.equal(form.action, `${hostedOrigin}/public/onboarding/setup/receive`); assert.equal(form.submitted, true);
  assert.deepEqual(form.children.map((value) => value.name), ['journey_id', 'request']);
  assert.deepEqual(JSON.parse(form.children[1].value), payload.request);
  assert.throws(() => submitReceivingEntry(run.document, { ...payload, hosted_start_url: 'https://other.example/receive' }, hostedOrigin));
});

test('receiving target stays separate from the independently observed account', () => {
  const run = page();
  renderReceivingContext(run.document, { intended_creator_id: 'creator-a', current_creator_id: 'creator-b' });
  assert.equal(run.node('setup-transfer-intended').textContent, 'Account name unavailable');
  assert.equal(run.node('setup-transfer-current').textContent, 'Account name unavailable');
  assert.equal(run.node('setup-transfer-mismatch').classList.values.has('hidden'), false);
  renderReceivingContext(run.document, { intended_creator_id: 'creator-a', current_creator_id: null });
  assert.equal(run.node('setup-transfer-current').textContent, 'Not identified');
  assert.equal(run.node('setup-transfer-mismatch').classList.values.has('hidden'), true);
  renderReceivingContext(run.document, null);
  assert.equal(run.node('setup-transfer-creator').classList.values.has('hidden'), true);
});
test('code validation stays on the receiving screen and preserves input on an unknown response', async () => {
  const run = page(); let calls = 0;
  bindSetupTransfer({ document: run.document, hostedOrigin, eligible: () => true,
    send: async () => { calls++; throw new Error('lost response'); } });
  run.node('setup-transfer-open').handlers.click();
  assert.equal(run.node('setup-transfer-code').focused, true);
  const submit = () => run.node('setup-transfer-form').handlers.submit({ preventDefault() {} });
  run.node('setup-transfer-code').value = '01234'; await submit();
  assert.equal(calls, 0); assert.equal(run.node('setup-transfer-status').textContent, 'Enter the 12-character setup code.');
  run.node('setup-transfer-code').value = '0123-4567-89AB'; await submit();
  assert.equal(calls, 1); assert.equal(run.node('setup-transfer-code').value, '0123-4567-89AB');
  assert.equal(run.node('setup-transfer-status').textContent, 'Setup could not be opened.');
  assert.equal(run.node('setup-transfer-submit').disabled, false); assert.equal(run.forms.length, 0);
});

test('the binding leaves visibility to its renderer and rejects an ineligible shortcut', () => {
  const run = page(); let eligible = false, changes = 0, restored = 0;
  run.node('setup-transfer-open').classList.values.add('hidden');
  const transfer = bindSetupTransfer({ document: run.document, hostedOrigin, send: async () => {},
    eligible: () => eligible, changed: () => { changes++; }, restoreFocus: () => { restored++; } });
  assert.equal(run.node('setup-transfer-open').classList.values.has('hidden'), true);
  run.node('setup-transfer-open').handlers.click();
  assert.equal(transfer.opened, false); assert.equal(changes, 0);
  eligible = true; run.node('setup-transfer-open').handlers.click();
  assert.equal(transfer.opened, true); assert.equal(changes, 1);
  run.node('setup-transfer-back').handlers.click();
  assert.equal(transfer.opened, false); assert.equal(changes, 2); assert.equal(restored, 1);
  assert.equal(run.node('setup-transfer-open').classList.values.has('hidden'), true);
  transfer.open({ direct: true });
  assert.equal(transfer.opened, true); assert.equal(transfer.direct, true);
  transfer.close({ notify: false, focus: false });
  assert.equal(transfer.direct, false); assert.equal(changes, 3); assert.equal(restored, 1);
});

test('an invalidated code-entry task ignores a late prepared navigation', async () => {
  const run = page(); let release;
  const response = new Promise((resolve) => { release = resolve; });
  const transfer = bindSetupTransfer({ document: run.document, hostedOrigin,
    eligible: () => true, send: () => response });
  transfer.open(); run.node('setup-transfer-code').value = '0123456789AB';
  const pending = run.node('setup-transfer-form').handlers.submit({ preventDefault() {} });
  transfer.close({ notify: false, focus: false });
  release(payload); await pending;
  assert.equal(run.forms.length, 0); assert.equal(transfer.opened, false);
});
