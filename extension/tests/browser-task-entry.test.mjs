import test from 'node:test';
import assert from 'node:assert/strict';
import { createBrowserTaskEntry, desktopAdopted, BROWSER_TASK_ENTRY_KEY } from '../runtime/browser-task-entry.mjs';
import { payload, id } from '../test-fixtures/browser-task.mjs';

function fixture() {
  const values = {}, task = payload().task;
  let clock = task.expires_at * 1000 - 1000;
  const record = { journey_id: task.journey_id, draft_scope: { scope_id: id(7), disclosure_bundle_id: 'a'.repeat(64) } };
  const storage = { async get(key) { return { [key]: values[key] }; }, async set(data) { Object.assign(values, structuredClone(data)); },
    async remove(key) { delete values[key]; } };
  return { values, task, record, storage, expire() { clock += 1001; }, entry: createBrowserTaskEntry({ storage, now: () => clock }) };
}

test('only the current scoped access task survives read and fixed expiry', async () => {
  const f = fixture();
  await f.entry.open(f.task, f.record.draft_scope, async () => {});
  assert.equal((await f.entry.read(f.record)).task.task_id, f.task.task_id);
  assert.equal(await f.entry.read({ ...f.record, journey_id: id(90) }), null);
  assert.equal(await f.entry.read({ ...f.record, draft_scope: { ...f.record.draft_scope, scope_id: id(91) } }), null);
  f.expire();
  assert.equal(await f.entry.read(f.record), null);
  assert.equal(f.values[BROWSER_TASK_ENTRY_KEY], undefined);
});

test('close during an in-flight write clears that task but cannot clear its replacement', async () => {
  const f = fixture(); let allow;
  const pending = f.entry.open(f.task, f.record.draft_scope, () => new Promise(resolve => { allow = resolve; }));
  await new Promise(resolve => setImmediate(resolve));
  const closing = f.entry.close(f.task);
  allow(); await pending; await closing;
  assert.equal(await f.entry.read(f.record), null);
  const next = { ...f.task, admission_generation: id(88) };
  await f.entry.open(next, f.record.draft_scope, async () => {});
  await f.entry.close(f.task);
  assert.equal((await f.entry.read(f.record)).task.admission_generation, id(88));
});

test('closed tasks, changed authority and expiry during authorization cannot open consent UI', async () => {
  const f = fixture();
  for (const change of [{ purpose: 'browser.authentication' }, { state: 'cancelled' }, { extra: true }])
    await assert.rejects(f.entry.open({ ...f.task, ...change }, f.record.draft_scope, async () => {}));
  await assert.rejects(f.entry.open(f.task, f.record.draft_scope, async () => { throw Error('closed'); }));
  await assert.rejects(f.entry.open(f.task, f.record.draft_scope, async () => f.expire()));
  assert.equal(f.values[BROWSER_TASK_ENTRY_KEY], undefined);
});

test('desktop adoption requires this workspace, disclosure and browser instance', () => {
  const f = fixture(), adoption = { journey_id: f.record.journey_id, draft_scope: f.record.draft_scope, instance_id: id(8) };
  assert.equal(desktopAdopted(adoption, f.record, id(8)), true);
  for (const bad of [null, {}, { ...adoption, journey_id: id(9) }, { ...adoption, instance_id: id(10) },
    { ...adoption, draft_scope: { ...adoption.draft_scope, disclosure_bundle_id: 'b'.repeat(64) } }, { ...adoption, success: true }])
    assert.equal(desktopAdopted(bad, f.record, id(8)), false);
});

test('a restarted worker retires its prior helper presentation before any read or new admission', async () => {
  const f = fixture();
  f.values.savedFullChoice = true;
  await f.entry.open(f.task, f.record.draft_scope, async () => {});
  const restarted = createBrowserTaskEntry({ storage: f.storage });
  assert.equal(await restarted.read(f.record), null);
  assert.equal(f.values[BROWSER_TASK_ENTRY_KEY], undefined);
  assert.equal(f.values.savedFullChoice, true);
  let authorized = false;
  await restarted.open(f.task, f.record.draft_scope, async () => { authorized = true; });
  assert.equal(authorized, true);
  assert.equal((await restarted.read(f.record)).task.task_id, f.task.task_id);
});
