import assert from 'node:assert/strict';
import { readFile, realpath } from 'node:fs/promises';
import { relative, isAbsolute } from 'node:path';
import { setImmediate } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { publicSigningError } from 'local-authenticated-read-connector/browser-signing';
import {
  createSignerReleaseFixture, deferred, EXPECTED_ID, PRIVATE_MARKER, RULE,
} from './signer-release-fixture.mjs';

async function established() {
  const fixture = createSignerReleaseFixture();
  const provider = await fixture.createProvider();
  await provider.read({ operation: 'identity', refreshMode: 'allow' });
  return { fixture, provider };
}

test('release contracts exercise the installed 0.2.0 public browser-signing export', async () => {
  const installed = await realpath(fileURLToPath(new URL('../node_modules/local-authenticated-read-connector/', import.meta.url)));
  const resolved = await realpath(fileURLToPath(import.meta.resolve('local-authenticated-read-connector/browser-signing')));
  const entry = relative(installed, resolved);
  assert.equal(isAbsolute(entry) || entry.startsWith('..'), false);
  const metadata = JSON.parse(await readFile(new URL('../node_modules/local-authenticated-read-connector/package.json', import.meta.url)));
  assert.equal(metadata.version, '0.2.0');
});

test('fresh absent-user-id capture validates the independently authorized account before activation', async () => {
  const f = createSignerReleaseFixture();
  const provider = await f.createProvider();
  const before = await provider.status();
  assert.equal(before.browser.tab_available, true);
  assert.equal(before.browser.ready, null);
  assert.equal(before.readiness.ready, false);
  assert.equal(f.calls.reads.length, 0);
  const result = await provider.read({ operation: 'identity', parameters: {}, refreshMode: 'allow' });
  assert.equal(result.success, true);
  assert.deepEqual(result.data, { id: EXPECTED_ID });
  assert.equal(result.refreshed, true);
  assert.equal(f.calls.captures[0]['user-id'], undefined);
  assert.equal(f.calls.reads.length, 3, 'cold read contains candidate, negative control, and requested read');
  assert.equal(f.calls.reloads, 1);
  assert.equal(f.calls.saves, 1);
  assert.equal(f.snapshot().active.proof_policy_version, 2);
  assert.equal(f.snapshot().active.context.session.userId, EXPECTED_ID);
  for (const request of f.calls.reads) assert.equal(request.headers['user-id'], EXPECTED_ID);
  assert.doesNotMatch(JSON.stringify([result, f.snapshot()]), new RegExp(PRIVATE_MARKER));
  for (const listener of Object.values(f.webRequest)) assert.equal(listener.size, 0);
  const status = await provider.status();
  assert.equal(status.readiness.current_proof, true);
  assert.equal(status.readiness.authenticated, null);
  assert.equal(status.readiness.ready, null);
  assert.equal(f.calls.reads.length, 3, 'status never authenticates by dispatching a read');
});

test('wrong authorized identity rejects the candidate before control or save', async () => {
  const f = createSignerReleaseFixture({ expectedIdentity: '9002' });
  const provider = await f.createProvider();
  await assert.rejects(provider.read({ operation: 'identity' }), { code: 'account_mismatch' });
  assert.equal(f.calls.reads.length, 1);
  assert.equal(f.calls.saves, 0);
  assert.equal(f.snapshot(), null);
});

test('missing independent identity and disabled refresh do not fabricate native bootstrap authorization', async () => {
  const f = createSignerReleaseFixture();
  const provider = await f.createProvider({ expectedIdentity: null });
  await assert.rejects(provider.read({ operation: 'identity', refreshMode: 'never' }), { code: 'refresh_required' });
  assert.equal(f.calls.reloads, 0);
  await assert.rejects(provider.read({ operation: 'identity' }), { code: 'packaged_rule_mismatch' });
  assert.equal(f.calls.reads.length, 0);
  assert.equal(f.calls.saves, 0);
});

test('legacy strong proof is retained until bounded signer-owned revalidation succeeds', async () => {
  const { fixture: f } = await established();
  // Synthetic prior-release fixture: omission models historical persistence, not a production migration edit.
  const legacy = f.snapshot();
  delete legacy.active.proof_policy_version;
  delete legacy.state_revision;
  f.replaceStoredState(legacy);
  const before = structuredClone(f.calls);
  const provider = await f.createProvider();
  assert.equal((await provider.status()).active.requires_validation, true);
  assert.deepEqual(f.snapshot(), legacy, 'load/status cannot rewrite historical proof');
  await assert.rejects(provider.read({ operation: 'identity', refreshMode: 'never' }), { code: 'refresh_required' });
  assert.equal(f.calls.reads.length, before.reads.length);
  assert.equal(f.calls.reloads, before.reloads);
  assert.equal(f.calls.saves, before.saves);
  const result = await provider.read({ operation: 'identity', refreshMode: 'allow' });
  assert.equal(result.success, true);
  assert.equal(f.calls.reads.length - before.reads.length, 3);
  assert.equal(f.calls.reloads - before.reloads, 1);
  assert.equal(f.calls.saves - before.saves, 1);
  assert.equal(f.snapshot().active.proof_policy_version, 2);
  assert.deepEqual(f.snapshot().previous, legacy.active);
});

test('failed revalidation cannot replace legacy last-known-good signing state', async () => {
  const { fixture: f } = await established();
  const legacy = f.snapshot();
  delete legacy.active.proof_policy_version;
  f.replaceStoredState(legacy);
  f.controlReply = () => f.response({ error: PRIVATE_MARKER }, 403);
  const before = structuredClone(f.calls);
  const provider = await f.createProvider();
  await assert.rejects(provider.read({ operation: 'identity' }), { code: 'activation_proof_failed' });
  assert.deepEqual(f.snapshot(), legacy);
  assert.equal(f.calls.saves, before.saves);
  assert.equal(f.calls.reads.length - before.reads.length, 2);
  assert.equal(f.calls.reloads - before.reloads, 1);
});

test('corrupt, unsupported, and unavailable account persistence fail without global storage fallback or overwrite', async (t) => {
  for (const [document, code] of [
    [{ schema: 'browser-signing-state/v1', active: { malformed: true }, previous: null }, 'storage_corrupt'],
    [{ schema: 'browser-signing-state/v99', active: null, previous: null }, 'storage_schema_unsupported'],
  ]) {
    await t.test(code, async () => {
      const f = createSignerReleaseFixture({ initialState: document });
      await assert.rejects(f.createProvider(), { code });
      assert.deepEqual(f.snapshot(), document);
      assert.equal(f.calls.saves, 0);
      assert.equal(f.calls.reloads, 0);
      assert.equal(f.calls.reads.length, 0);
    });
  }
  await t.test('transient load failure is retryable without erasing state', async () => {
    const { fixture: f } = await established();
    const snapshot = f.snapshot();
    const load = f.persistence.load;
    f.persistence.load = async () => { throw new Error(PRIVATE_MARKER); };
    await assert.rejects(f.createProvider(), { code: 'storage_load_failed' });
    f.persistence.load = load;
    const provider = await f.createProvider();
    const result = await provider.read({ operation: 'identity', refreshMode: 'never' });
    assert.equal(result.success, true);
    assert.deepEqual(f.snapshot(), snapshot);
    assert.equal(f.calls.reloads, 1);
    assert.equal(f.calls.saves, 1);
  });
});

test('an ambiguous save acknowledgement is reconciled from the saved whole document on reconstruction', async () => {
  const f = createSignerReleaseFixture();
  const save = f.persistence.save;
  f.persistence.save = async (document) => { await save(document); throw new Error(PRIVATE_MARKER); };
  const provider = await f.createProvider();
  await assert.rejects(provider.read({ operation: 'identity' }), (error) => {
    assert.deepEqual(publicSigningError(error), { failure_code: 'storage_save_failed', validation_error: null });
    return true;
  });
  const committed = f.snapshot();
  assert.equal(committed.active.proof_policy_version, 2);
  assert.equal(f.calls.reads.length, 2, 'the unacknowledged activation cannot authorize a requested read');
  f.persistence.save = save;
  const reconstructed = await f.createProvider();
  const result = await reconstructed.read({ operation: 'identity', refreshMode: 'never' });
  assert.equal(result.generation_id, committed.active.id);
  assert.equal(result.success, true);
  assert.equal(f.calls.reads.length, 3);
  assert.equal(f.calls.reloads, 1);
  assert.equal(f.calls.saves, 1);
});

test('ordinary HTTP failures retain safe codes and 429 timing without spending a refresh or reload budget', async (t) => {
  for (const [status, code] of [[400, 'upstream_rejected'], [401, 'authorization_failed'],
    [403, 'authorization_failed'], [429, 'rate_limited'], [503, 'upstream_unavailable']]) {
    await t.test(String(status), async () => {
      const { fixture: f, provider } = await established();
      const saved = f.snapshot(); const before = f.calls.reads.length;
      const events = [];
      provider.onDiagnostic = (event) => events.push(event);
      f.reply = () => f.response({ error: PRIVATE_MARKER }, status, {
        responseRevision: null, retryAfterMs: status === 429 ? 2500 : null,
        contentType: `application/json; private=${PRIVATE_MARKER}`,
      });
      const result = await provider.read({ operation: 'conversations', refreshMode: 'allow' });
      assert.equal(result.success, false);
      assert.equal(result.data, null);
      assert.equal(result.response.failure_code, code);
      assert.equal(result.response.retry_after_ms, status === 429 ? 2500 : null);
      assert.equal(result.refreshed, false);
      assert.equal(f.calls.reads.length - before, 1);
      assert.equal(f.calls.reloads, 1);
      assert.equal(f.calls.saves, 1);
      assert.deepEqual(f.snapshot(), saved);
      assert.doesNotMatch(JSON.stringify([result, events]), new RegExp(PRIVATE_MARKER));
    });
  }
});

test('ordinary authorization/rate-limit failures cannot serve as a successful negative proof', async (t) => {
  for (const status of [401, 429]) {
    await t.test(String(status), async () => {
      const f = createSignerReleaseFixture();
      f.controlReply = () => f.response({ error: PRIVATE_MARKER }, status);
      const provider = await f.createProvider();
      await assert.rejects(provider.read({ operation: 'identity' }), { code: 'activation_proof_failed' });
      assert.equal(f.calls.reads.length, 2);
      assert.equal(f.calls.saves, 0);
      assert.equal(f.snapshot(), null);
    });
  }
});

test('malformed Chrome response metadata rejects without capture or persistence repair', async () => {
  const { fixture: f, provider } = await established();
  f.reply = () => f.response({ list: [], hasMore: false }, '200');
  await assert.rejects(provider.read({ operation: 'conversations', refreshMode: 'allow' }), (error) => {
    // The real Chrome host rejects malformed injection results before response parsing.
    assert.deepEqual(publicSigningError(error), { failure_code: 'signing_failed', validation_error: null });
    return true;
  });
  assert.equal(f.calls.reloads, 1);
  assert.equal(f.calls.saves, 1);
});

test('an unsupported packaged revision remains an error and preserves the prior signing document', async () => {
  const { fixture: f } = await established();
  const saved = f.snapshot();
  const provider = await f.createProvider({ packagedRule: { ...RULE, source_revision: 'unavailable-synthetic-revision' } });
  await assert.rejects(provider.read({ operation: 'identity' }), { code: 'unsupported_revision' });
  assert.deepEqual(f.snapshot(), saved);
  assert.equal(f.calls.saves, 1);
});

test('arbitrary pre-abort reasons are returned exactly before any browser work', async () => {
  const { fixture: f, provider } = await established();
  const before = structuredClone(f.calls);
  for (const reason of [null, false, 0, 'synthetic-cancel', Object.freeze({ synthetic_cancel: true })]) {
    const abort = new AbortController(); abort.abort(reason);
    await assert.rejects(provider.read({ operation: 'identity', signal: abort.signal }), (error) => Object.is(error, reason));
  }
  assert.deepEqual(f.calls, before);
});

test('in-flight non-Error cancellation abandons a read and its late response cannot escape', { timeout: 2000 }, async () => {
  const { fixture: f, provider } = await established();
  const entered = deferred(); const release = deferred();
  f.reply = () => { entered.resolve(); return release.promise; };
  const abort = new AbortController(); const reason = Object.freeze({ stopped: 'synthetic-account-change' });
  const read = provider.read({ operation: 'conversations', signal: abort.signal });
  const rejection = assert.rejects(read, (error) => error === reason);
  await entered.promise; abort.abort(reason); await rejection;
  release.resolve(f.response({ list: [], hasMore: false }));
  await setImmediate();
  assert.equal(f.calls.aborts.length, 1);
  assert.equal(f.calls.saves, 1);
  assert.equal(f.calls.reloads, 1);
});

test('cancellation during entered persistence save may commit signing state but cannot return a requested page', { timeout: 2000 }, async () => {
  const f = createSignerReleaseFixture();
  const entered = deferred(); const release = deferred(); const saved = deferred();
  const save = f.persistence.save;
  f.persistence.save = async (document) => {
    entered.resolve(); await release.promise; await save(document); saved.resolve();
  };
  const provider = await f.createProvider();
  const abort = new AbortController(); const reason = 'synthetic-abandon-during-save';
  const read = provider.read({ operation: 'conversations', signal: abort.signal });
  const rejection = assert.rejects(read, (error) => error === reason);
  await entered.promise;
  assert.equal(f.snapshot(), null);
  abort.abort(reason); await rejection;
  release.resolve(); await saved.promise;
  assert.equal(f.snapshot().active.proof_policy_version, 2);
  assert.equal(f.calls.reads.length, 2, 'only completed candidate/control reads exist, never a late requested page');
  f.persistence.save = save;
  const reconstructed = await f.createProvider();
  const result = await reconstructed.read({ operation: 'conversations', refreshMode: 'never' });
  assert.equal(result.success, true);
  assert.equal(f.calls.saves, 1);
  assert.equal(f.calls.reloads, 1);
});

test('document replacement during a logical read fails closed without saving or recapturing', async () => {
  const { fixture: f, provider } = await established();
  const saved = f.snapshot();
  f.reply = () => { f.navigate(); return f.response({ list: [], hasMore: false }); };
  await assert.rejects(provider.read({ operation: 'conversations' }), { code: 'context_lost' });
  assert.deepEqual(f.snapshot(), saved);
  assert.equal(f.calls.saves, 1);
  assert.equal(f.calls.reloads, 1);
});

test('the consumer frozen-tab guard and signer unsafe-refresh policy both prevent MAIN-world reads', async (t) => {
  await t.test('frozen tab', async () => {
    const { fixture: f, provider } = await established();
    f.tab.frozen = true;
    const before = f.calls.reads.length;
    await assert.rejects(provider.read({ operation: 'conversations' }));
    assert.equal(f.calls.reads.length, before);
    assert.equal(f.calls.reloads, 1);
  });
  await t.test('draft prevents cold reload', async () => {
    const f = createSignerReleaseFixture();
    f.safeRefresh = { safe: false, reason: 'composer_draft_present' };
    const provider = await f.createProvider();
    await assert.rejects(provider.read({ operation: 'identity' }), { code: 'refresh_unsafe' });
    assert.equal(f.calls.reads.length, 0);
    assert.equal(f.calls.reloads, 0);
    assert.equal(f.calls.saves, 0);
  });
});
