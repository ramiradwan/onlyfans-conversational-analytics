import assert from 'node:assert/strict';
import test from 'node:test';
import { openPairingStore } from '../runtime/companion-pairing-store.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

test('explicit pairing replacement can forget only the exact saved pin presented for confirmation', async () => {
  const indexedDB = new FakeIndexedDb();
  const store = await openPairingStore({ indexedDB, name: 'replacement-fixture' });
  const root = indexedDB.databases.get('replacement-fixture').stores.get('state').records.get('root');
  root.pin = { pairingId: 'first', identity: { installation_id: 'installation-one' } };
  root.highWater['installation-one'] = 3;
  const expectedToken = await store.savedPairing();
  root.pin = { pairingId: 'second', identity: { installation_id: 'installation-two' } };
  await assert.rejects(store.forget({ expectedToken }), { code: 'pairing_state_refused' });
  assert.equal((await store.status()).paired, true);
  const current = await store.savedPairing();
  const aborted = new AbortController(); aborted.abort();
  await assert.rejects(store.forget({ expectedToken: current, signal: aborted.signal }));
  assert.equal((await store.status()).paired, true);
  await store.forget({ expectedToken: current });
  assert.equal((await store.status()).paired, false);
  assert.equal((await store.status()).highWater['installation-one'], 3);
  assert.equal(await store.savedPairing(), null);
  store.close();
});
