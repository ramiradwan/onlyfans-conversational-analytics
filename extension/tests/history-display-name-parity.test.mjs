import assert from 'node:assert/strict';
import test from 'node:test';

import { parseTypedResponse } from 'local-authenticated-read-connector/browser-signing';

import { normalizeChatRecord } from '../capture/normalization.mjs';
import { mapPlatformObservation } from '../transport/capture-ingestion.mjs';
import { mapPlatformObservation as mapReadOnlyPlatformObservation } from '../transport/read-only-capture-ingestion.mjs';
import { mergeChat } from '../transport/entity-merge.mjs';
import { normalizeSignerConversation } from '../transport/signer-normalization.mjs';
import { normalizeSignerConversation as normalizeReadOnlySignerConversation } from '../transport/read-only-signer-normalization.mjs';

const CREATOR = '100200300';
const OBSERVED_AT = '2026-09-20T06:00:00.000Z';

// Full upstream conversation object. The development simulator emits `name`
// only, so every fixture-based suite agrees by construction; real records carry
// `displayName`, `name` and `username` together and they routinely disagree.
const upstreamConversation = () => ({
  id: '77001',
  withUser: {
    id: '77001',
    name: 'Fan Account',
    displayName: 'fan.account',
    username: 'fanaccount',
  },
  lastMessage: { createdAt: '2026-09-19T21:14:00Z' },
});

function passiveChat(record, map = mapPlatformObservation) {
  // page-hook.js reduces the record at the page boundary, then the worker maps it.
  const reduced = normalizeChatRecord(record, OBSERVED_AT);
  assert.notEqual(reduced, null);
  const mapped = map({
    event_type: 'chat.observed',
    observed_at: OBSERVED_AT,
    source_path: '/api2/v2/chats',
    creator_platform_user_id: CREATOR,
    context_chat_id: null,
    record: reduced,
  });
  assert.equal(mapped.ok, true);
  return mapped.change.chat;
}

function signerChat(record, normalize = normalizeSignerConversation) {
  const page = parseTypedResponse({
    operation: 'conversations',
    status: 200,
    contentType: 'application/json',
    body: { list: [record], hasMore: false },
  });
  assert.equal(page.summary.semantic_success, true);
  return normalize(page.data.items[0], {
    observedAt: OBSERVED_AT,
    creatorPlatformId: CREATOR,
  }).chat;
}

const variants = [
  ['authoring', mapPlatformObservation, normalizeSignerConversation],
  ['read-only', mapReadOnlyPlatformObservation, normalizeReadOnlySignerConversation],
];

for (const [name, map, normalize] of variants) {
  test(`${name}: passive and signer capture agree on the conversation display name`, () => {
    const record = upstreamConversation();
    const passive = passiveChat(record, map);
    const signer = signerChat(record, normalize);
    assert.equal(
      passive.display_name,
      signer.display_name,
      'passive and signer paths must resolve the same upstream display-name alias',
    );
  });

  test(`${name}: the same conversation seen twice does not raise material_conflict`, () => {
    const record = upstreamConversation();
    const passive = passiveChat(record, map);
    const signer = signerChat(record, normalize);
    assert.equal(passive.updated_at, signer.updated_at);
    // Signer inventory reading a chat the page hook already captured is the
    // exact real-account sequence that closed 22 inventory jobs.
    assert.deepEqual(mergeChat(passive, signer), { action: 'noop', value: passive });
    assert.deepEqual(mergeChat(signer, passive), { action: 'noop', value: signer });
  });
}

test('every nested user alias the connector accepts resolves identically', () => {
  const aliases = [
    { displayName: 'a', name: 'b', username: 'c' },
    { name: 'b', username: 'c' },
    { displayName: 'a', username: 'c' },
    { username: 'c' },
    { displayName: 'a' },
    { name: 'b' },
  ];
  for (const user of aliases) {
    const record = { id: '77002', withUser: { id: '77002', ...user }, lastMessage: { createdAt: '2026-09-19T21:14:00Z' } };
    assert.equal(
      passiveChat(record).display_name,
      signerChat(record).display_name,
      `alias precedence diverges for ${JSON.stringify(user)}`,
    );
  }
});

test('an explicit display_name alias still wins over nested user fields', () => {
  const record = {
    id: '77003',
    display_name: 'Explicit',
    withUser: { id: '77003', name: 'Fan Account', displayName: 'fan.account' },
    lastMessage: { createdAt: '2026-09-19T21:14:00Z' },
  };
  assert.equal(passiveChat(record).display_name, 'Explicit');
  assert.equal(signerChat(record).display_name, 'Explicit');
});
