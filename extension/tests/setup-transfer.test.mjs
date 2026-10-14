import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createSetupTransferOwner, openSetupTransferStore, canonicalTransfer, normalizeSetupCode, transferProofBytes,
  registerSetupTransfer, SETUP_TRANSFER_MESSAGE } from '../runtime/setup-transfer.mjs';
import { b64u, digest, toHex, unb64u, lowSignature } from '../transport/pairing-contract.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const journey = '11111111-1111-4111-8111-111111111111';
const start = Date.parse('2026-10-08T12:00:00Z');
async function challenge(request, at = start, nonce = new Uint8Array(32).fill(1)) {
  return { profile: 'urn:bridge-clean:onboarding-proof:v1', challenge: b64u(nonce),
    expires_at: new Date(at + 60_000).toISOString(), request_digest: toHex(await digest(new TextEncoder().encode(canonicalTransfer(request)))) };
}
test('transfer proof bytes match the independently published vector', async () => {
  const vector = JSON.parse(readFileSync(new URL('../../contracts/onboarding-continuity-v1/proof-cases.json', import.meta.url)))
    .find((value) => value.case_id === 'valid');
  const proof = await challenge(vector.request, start, unb64u(vector.challenge));
  assert.equal(toHex(await transferProofBytes(vector.request, proof)), vector.proof_bytes_hex);
});
test('code normalization accepts formatting without guessing ambiguous characters', () => {
  assert.equal(normalizeSetupCode('0123-4567-89ab'), '0123456789AB');
  for (const invalid of ['O123456789AB', 'I123456789AB', '012345', '0123456789ABextra']) assert.equal(normalizeSetupCode(invalid), null);
});
test('nonexportable dedicated key and exact request survive worker recreation without source authority', async () => {
  const store = await openSetupTransferStore(new FakeIndexedDb());
  try {
    const first = createSetupTransferOwner({ store, now: () => start });
    const request = await first.prepare(journey, '0123-4567-89ab');
    const record = await store.transaction((state) => ({ state, result: state }));
    assert.equal(record.keys.privateKey.extractable, false);
    await assert.rejects(crypto.subtle.exportKey('jwk', record.keys.privateKey));
    const second = createSetupTransferOwner({ store, now: () => start + 1000 });
    assert.deepEqual(await second.prepare(journey, '0123456789AB'), request);
    const proof = await challenge(request);
    const signed = await second.sign(journey, request, proof);
    const raw = unb64u(signed.signature); lowSignature(raw);
    const key = await crypto.subtle.importKey('jwk', request.destination.public_key, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
    assert.equal(await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, raw, await transferProofBytes(request, proof)), true);
    assert.deepEqual(Object.keys(signed).sort(), ['challenge', 'signature']);
    await assert.rejects(second.sign(journey, request, proof), /unavailable/u);
  } finally { store.close(); }
});
test('foreign request, changed destination, stale challenge and expired draft cannot sign', async () => {
  let time = start;
  const store = await openSetupTransferStore(new FakeIndexedDb());
  try {
    const owner = createSetupTransferOwner({ store, now: () => time });
    const request = await owner.prepare(journey, '0123456789AB');
    const proof = await challenge(request);
    for (const changed of [{ ...request, setup_code: '1123456789AB' },
      { ...request, destination: { ...request.destination, kind: 'desktop' } },
      { ...request, authority: 'installation' }]) await assert.rejects(owner.sign(journey, changed, proof));
    await assert.rejects(owner.sign('22222222-2222-4222-8222-222222222222', request, proof));
    await assert.rejects(owner.sign(journey, request, { ...proof, request_digest: '0'.repeat(64) }));
    time += 60_000;
    await assert.rejects(owner.sign(journey, request, proof));
    time = start + 30 * 60_000;
    await owner.prune();
    assert.equal(await store.transaction((state) => ({ state, result: state })), null);
    await assert.rejects(owner.sign(journey, request, await challenge(request, time)));
  } finally { store.close(); }
});

test('continuation resumes only the matching signed workflow without importing grants or consent', async () => {
  const store = await openSetupTransferStore(new FakeIndexedDb());
  const owner = createSetupTransferOwner({ store, now: () => start });
  const continuation = { profile: 'urn:bridge-clean:onboarding-continuation:v1', reference: 'r'.repeat(43),
    return_target: 'extension-setup', expires_at: new Date(start + 60_000).toISOString() };
  try {
    const request = await owner.prepare(journey, '0123456789AB');
    assert.equal(await owner.context(journey), null);
    await assert.rejects(owner.resume(journey, continuation, 'creator-1'));
    await owner.sign(journey, request, await challenge(request));
    for (const value of [{ ...continuation, return_target: 'desktop-setup' }, { ...continuation, grants: [] },
      { ...continuation, expires_at: new Date(start).toISOString() }]) await assert.rejects(owner.resume(journey, value, 'creator-1'));
    await owner.resume(journey, continuation, 'creator-1');
    const saved = await store.transaction((state) => ({ state, result: state }));
    assert.equal(saved.intended_creator_id, 'creator-1');
    assert.deepEqual(saved.continuation, continuation);
    assert.equal(saved.expires, start + 30 * 60_000);
    assert.deepEqual(await owner.context(journey), { intended_creator_id: 'creator-1', expires_at: saved.expires });
    assert.equal(await owner.context('22222222-2222-4222-8222-222222222222'), null);
    const expired = createSetupTransferOwner({ store, now: () => saved.expires });
    assert.equal(await expired.context(journey), null);
    for (const authority of ['consent', 'grants', 'paired', 'account_verified']) assert.equal(Object.hasOwn(saved, authority), false);
  } finally { store.close(); }
});
test('only current admitted extension prepares and hosted workspace signs', async () => {
  const listeners = []; const store = await openSetupTransferStore(new FakeIndexedDb());
  let route = 'extension', admitted = true;
  const chromeApi = { alarms: { create() {}, onAlarm: { addListener() {} } }, runtime: {
    onMessage: { addListener(fn) { listeners.push(fn); } }, onMessageExternal: { addListener() {} },
  } };
  registerSetupTransfer({ chromeApi, workspace: { async admit() {
    if (!admitted) throw new Error('not admitted'); return { route, record: { journey_id: journey } };
  } }, hostedOrigin: 'https://setup.example.com', openStore: async () => store });
  const call = (message) => new Promise((resolve) => listeners[0](message, {}, resolve));
  try {
    const prepared = await call({ type: SETUP_TRANSFER_MESSAGE, action: 'prepare', setup_code: '0123456789AB' });
    assert.equal(prepared.ok, true);
    assert.equal(prepared.result.hosted_start_url, 'https://setup.example.com/public/onboarding/setup/receive');
    const message = { type: SETUP_TRANSFER_MESSAGE, action: 'sign', request: prepared.result.request,
      challenge: await challenge(prepared.result.request, Date.now()) };
    assert.equal((await call(message)).ok, false, 'extension page cannot pose as the authenticated hosted flow');
    route = 'hosted'; admitted = false;
    assert.equal((await call(message)).ok, false);
    admitted = true;
    assert.equal((await call(message)).ok, true);
  } finally { store.close(); }
});
