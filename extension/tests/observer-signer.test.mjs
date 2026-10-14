import assert from 'node:assert/strict';
import test from 'node:test';
import { createObserverSigningProvider } from '../runtime/observer-signer.mjs';
import { DocumentObserverCoordinator, OBSERVER_HELPER_KEY } from '../runtime/document-observer.mjs';
import { createLazyAccountSigner } from '../transport/agent-runtime-core.mjs';
import { SIGNER_REFRESH_BUDGET_KEY } from '../transport/signer-refresh-budget.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { createSignerReleaseFixture, EXPECTED_ID } from './signer-release-fixture.mjs';

async function harness({ observeExisting = false } = {}) {
  const fixture = createSignerReleaseFixture({ ownedHelpers: true, observeExisting });
  fixture.tab.url = 'https://onlyfans.com/my/chats';
  fixture.tab.active = true;
  const local = {}, session = {};
  const area = values => ({
    async get(keys) { return Object.fromEntries(keys.map(key => [key, structuredClone(values[key])])); },
    async set(update) { Object.assign(values, structuredClone(update)); },
  });
  const storageApi = { local: area(local), session: area(session) };
  let permission = true;
  const chromeApi = new Proxy(fixture.chromeApi, {
    get(target, property, receiver) {
      if (property === 'storage') return storageApi;
      if (property === 'permissions') return { contains: async () => permission };
      return Reflect.get(target, property, receiver);
    },
  });
  const observer = new DocumentObserverCoordinator({ chromeApi });
  observer.mode = 'full'; observer.start();
  await observer.discover();
  const storage = new InMemoryIngestionStorage();
  const lifetime = new AbortController();
  let constructions = 0;
  const restart = (owner = observer) => createLazyAccountSigner({
    creatorAccountId: 'application-account', storage, chromeApi,
    expectedIdentity: () => EXPECTED_ID, signal: lifetime.signal,
    factory: options => createObserverSigningProvider(options, {
      observer: owner,
      createProvider: values => { constructions++; return fixture.createProvider(values); },
      observationWindowMs: 20,
    }),
  });
  return { fixture, observer, local, session, storage, lifetime, chromeApi, restart,
    signer: restart(), constructions: () => constructions, denyPermission() { permission = false; } };
}
const request = signal => ({ operation: 'identity', parameters: {}, refreshMode: 'allow', signal });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('a silent user document bootstraps one helper with observation registered before navigation', async () => {
  const h = await harness();
  const original = h.fixture.currentDocument();
  const result = await h.signer.read(request());
  assert.equal(result.success, true);
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.fixture.calls.created[0].active, false);
  assert.match(h.fixture.calls.created[0].url, /^chrome-extension:\/\/[a-p]{32}\/signing-helper\.html$/);
  assert.deepEqual(h.fixture.calls.navigations, [{ id: 31, url: 'https://onlyfans.com/' }]);
  assert.equal(h.fixture.calls.captures.length, 1);
  assert.equal(h.fixture.calls.reloads, 0);
  assert.equal(h.fixture.currentDocument(), original);
  assert.equal(h.local[OBSERVER_HELPER_KEY].tab_id, 31);
  assert.equal(h.constructions(), 1, 'one provider and one generation writer');
  const budget = await h.storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', SIGNER_REFRESH_BUDGET_KEY));
  assert.equal(budget.attempts.length, 1);
  assert.equal((await h.signer.read(request())).success, true);
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.constructions(), 1);
});

test('a native observation in the existing tab needs no helper or recovery reservation', async () => {
  const h = await harness({ observeExisting: true });
  assert.equal((await h.signer.read(request())).success, true);
  assert.equal(h.fixture.calls.created.length, 0);
  assert.equal(await h.storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', SIGNER_REFRESH_BUDGET_KEY)), undefined);
});

test('Chrome callback completion cannot outrun helper ownership and initial navigation', async () => {
  const h = await harness();
  const create = h.chromeApi.tabs.create.bind(h.chromeApi.tabs);
  h.chromeApi.tabs.create = (options, callback) => {
    const pending = create(options);
    if (typeof callback !== 'function') return pending;
    void pending.then(tab => setImmediate(() => callback(tab)));
    return undefined;
  };
  assert.equal((await h.signer.read(request())).success, true);
  assert.equal(h.observer.signingCreation, null);
  assert.equal(h.local[OBSERVER_HELPER_KEY].tab_id, 31);
  assert.deepEqual(h.fixture.calls.navigations, [{ id: 31, url: 'https://onlyfans.com/' }]);
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.fixture.calls.reloads, 0);
});

test('a same-document platform route change retains the exact document proof', async () => {
  const h = await harness();
  const update = h.chromeApi.tabs.update;
  h.chromeApi.tabs.update = async (id, changes) => {
    const result = await update(id, changes);
    const tab = h.fixture.helperTabs.get(id);
    tab.url = 'https://onlyfans.com/my/chats';
    h.chromeApi.tabs.onUpdated.emit(id, { url: tab.url }, tab);
    return result;
  };
  assert.equal((await h.signer.read(request())).success, true);
  assert.equal(h.fixture.calls.navigations.length, 1);
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.fixture.calls.reloads, 0);
});

test('worker reconstruction uses the proved generation without another helper', async () => {
  const h = await harness();
  assert.equal((await h.signer.read(request())).success, true);
  const next = new DocumentObserverCoordinator({ chromeApi: h.chromeApi });
  next.mode = 'full'; next.start(); await next.discover();
  assert.equal(next.helper.tab_id, 31);
  assert.equal((await h.restart(next).read(request())).success, true);
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.fixture.calls.reloads, 0);
});

test('wrong account during helper bootstrap never activates a generation', async () => {
  const h = await harness();
  h.fixture.accountId = '9002';
  await assert.rejects(h.signer.read(request()), error => ['account_mismatch', 'activation_proof_failed'].includes(error.code));
  const record = await h.storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', 'signer-state'));
  assert.equal(record?.state.active ?? null, null);
  assert.equal(h.fixture.calls.created.length, 1);
});

for (const reason of ['paused', 'permission', 'closed']) {
  test(`${reason} state blocks helper recovery after a silent observation`, async () => {
    const h = await harness();
    if (reason === 'paused') h.observer.paused = true;
    if (reason === 'permission') h.denyPermission();
    if (reason === 'closed') h.observer.helper = { closed: true, tab_id: null };
    await assert.rejects(h.signer.read(request()), { code: 'history_unavailable' });
    assert.equal(h.fixture.calls.created.length, 0);
    assert.equal(h.fixture.calls.navigations.length, 0);
  });
}

test('cancelling observation does not create a helper or reserve recovery', async () => {
  const h = await harness();
  const cancelled = new AbortController();
  const pending = h.signer.read(request(cancelled.signal));
  const rejected = assert.rejects(pending, error => error === 'cancelled');
  await h.fixture.captureReady.promise;
  cancelled.abort('cancelled');
  await rejected;
  await tick();
  assert.equal(h.fixture.calls.created.length, 0);
  assert.equal(await h.storage.runTransaction('readonly', ['credentials'], tx => tx.get('credentials', SIGNER_REFRESH_BUDGET_KEY)), undefined);
});

test('history replaces only the untouched observer helper and keeps concurrent reads on one provider', async () => {
  const h = await harness();
  await h.observer.openHelper(h.observer.generation);
  assert.equal(h.observer.helper.tab_id, 31);
  assert.deepEqual(h.fixture.calls.navigations, [{ id: 31, url: 'https://onlyfans.com/' }]);
  const results = await Promise.all([h.signer.read(request()), h.signer.read(request())]);
  assert.ok(results.every(result => result.success));
  assert.deepEqual(h.fixture.calls.removed, [31]);
  assert.equal(h.fixture.helperTabs.size, 1);
  assert.equal(h.local[OBSERVER_HELPER_KEY].tab_id, 32);
  assert.equal(h.local[OBSERVER_HELPER_KEY].closed, false);
  assert.equal(h.fixture.calls.created.length, 2);
  assert.equal(h.constructions(), 1);
  assert.equal(h.fixture.currentDocument(), 'synthetic-document-0');
  assert.equal(h.fixture.calls.reloads, 0);
});

for (const action of ['activate', 'navigate', 'close']) {
  test(`a helper the customer chooses to ${action} is not replaced automatically`, async () => {
    const h = await harness();
    await h.observer.openHelper(h.observer.generation);
    if (action === 'activate') {
      h.fixture.helperTabs.get(31).active = true;
      h.chromeApi.tabs.onActivated.emit({ tabId: 31 });
    }
    if (action === 'navigate') {
      h.fixture.helperTabs.get(31).url = 'https://onlyfans.com/my/chats';
      h.chromeApi.tabs.onUpdated.emit(31, { url: 'https://onlyfans.com/my/chats' }, h.fixture.helperTabs.get(31));
    }
    if (action === 'close') await h.chromeApi.tabs.remove(31);
    await assert.rejects(h.signer.read(request()), { code: 'history_unavailable' });
    assert.equal(h.fixture.calls.created.length, 1);
    assert.equal(h.fixture.calls.navigations.length, 1);
    assert.deepEqual(h.fixture.calls.removed, action === 'close' ? [31] : []);
  });
}

for (const deleting of [false, true]) {
  test(`late helper creation after cancellation ${deleting ? 'honors deletion' : 'retains ownership across restart'} without navigating`, async () => {
    const h = await harness();
    const actualCreate = h.chromeApi.tabs.create.bind(h.chromeApi.tabs);
    let acknowledge, started;
    const entered = new Promise(resolve => { started = resolve; });
    h.chromeApi.tabs.create = (options) => new Promise(resolve => {
      acknowledge = async () => resolve(await actualCreate(options));
      started();
    });
    const cancelled = new AbortController();
    const read = h.signer.read(request(cancelled.signal));
    const rejected = assert.rejects(read, error => error === 'cancelled');
    await entered;
    const creation = h.observer.signingCreation;
    cancelled.abort('cancelled');
    await rejected;
    await h.observer.queue;
    assert.equal(h.observer.signingCreation, creation);
    await h.observer.openHelper(h.observer.generation);
    assert.equal(h.fixture.calls.created.length, 0);
    if (deleting) {
      await h.observer.resetForDeletion();
      for (const key of Object.keys(h.local)) delete h.local[key];
    }
    await acknowledge();
    await creation;
    await tick();
    assert.equal(h.fixture.calls.created.length, 1);
    assert.equal(h.fixture.calls.navigations.length, 0);
    assert.equal(h.observer.signingCreation, null);
    if (deleting) {
      assert.deepEqual(h.local, {});
      assert.deepEqual(h.fixture.calls.removed, [31]);
    } else {
      assert.equal(h.local[OBSERVER_HELPER_KEY].tab_id, 31);
      const restarted = new DocumentObserverCoordinator({ chromeApi: h.chromeApi });
      await restarted.discover();
      assert.equal(restarted.helper.tab_id, 31);
      assert.equal(h.fixture.calls.created.length, 1);
      assert.deepEqual(h.fixture.calls.removed, []);
    }
  });
}

test('an exhausted recovery budget cannot remove or replace the existing helper', async () => {
  const h = await harness();
  await h.observer.openHelper(h.observer.generation);
  await h.storage.runTransaction('readwrite', ['credentials'], tx => tx.put('credentials', {
    key: SIGNER_REFRESH_BUDGET_KEY, creator_account_id: 'application-account', attempts: [Date.now()],
  }));
  await assert.rejects(h.signer.read(request()), { code: 'history_refresh_backoff' });
  assert.equal(h.fixture.calls.created.length, 1);
  assert.deepEqual(h.fixture.calls.removed, []);
  assert.equal(h.observer.helper.tab_id, 31);
});

test('pausing after helper creation cancels the pending document proof before platform navigation', async () => {
  const h = await harness();
  let release, entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const originalContexts = h.chromeApi.runtime.getContexts;
  h.chromeApi.runtime.getContexts = async (...args) => {
    entered();
    await new Promise(resolve => { release = resolve; });
    return originalContexts(...args);
  };
  const pending = h.signer.read(request());
  const rejected = assert.rejects(pending, { code: 'observer_changed' });
  await waiting;
  const paused = h.observer.configure('full', true);
  await rejected;
  release();
  await paused;
  await tick();
  assert.equal(h.fixture.calls.created.length, 1);
  assert.equal(h.fixture.calls.navigations.length, 0);
  assert.equal(h.observer.helper.tab_id, 31);
  assert.equal(h.local[OBSERVER_HELPER_KEY].closed, false);
});

test('a delayed ownership write cannot erase a later customer activation', async () => {
  const h = await harness();
  const set = h.chromeApi.storage.local.set;
  let release, entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  let held = false;
  h.chromeApi.storage.local.set = async update => {
    if (!held && update[OBSERVER_HELPER_KEY]?.tab_id === 31) {
      held = true; entered();
      await new Promise(resolve => { release = resolve; });
    }
    await set(update);
  };
  const pending = h.signer.read(request());
  const rejected = assert.rejects(pending, { code: 'observer_changed' });
  await waiting;
  h.fixture.helperTabs.get(31).active = true;
  h.chromeApi.tabs.onActivated.emit({ tabId: 31 });
  await rejected;
  release();
  await h.observer.helperWriteTail;
  assert.equal(h.local[OBSERVER_HELPER_KEY].touched, true);
  assert.equal(h.fixture.calls.navigations.length, 0);
});
