import assert from 'node:assert/strict';
import test from 'node:test';

import { INGESTION_STORES } from '../transport/durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from '../transport/indexeddb-ingestion-storage.mjs';
import { createReadOnlyIndexedDbIngestionStorage } from '../transport/read-only-indexeddb-ingestion-storage.mjs';
import { FakeIndexedDb } from './fake-indexeddb.mjs';

const KEY_A = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
const KEY_B = 'ERERERERERERERERERERERERERERERERERERERERERE=';

function serializedRecords(indexedDb, databaseName) {
  return JSON.stringify([...indexedDb.databases.get(databaseName).stores].map(
    ([storeName, store]) => [storeName, [...store.records]],
  ));
}

for (const [name, factory] of [
  ['authoring graph', createIndexedDbIngestionStorage],
  ['read-only Store graph', createReadOnlyIndexedDbIngestionStorage],
]) {
  test(`${name} encrypts values and private lookup keys while preserving indexed reads`, async () => {
    const indexedDb = new FakeIndexedDb();
    const databaseName = `encrypted-${name}`;
    const storage = factory(indexedDb, { databaseName, encryptionKey: KEY_A });
    const chat = {
      chat_id: 'private-chat-1',
      display_name: 'Private Fan Name',
      platform_user_id: 'private-fan-1',
    };
    const message = {
      message_id: 'private-message-1',
      chat_id: chat.chat_id,
      text: 'plaintext must never survive in IndexedDB',
    };
    const credential = {
      key: 'signer-state',
      creator_account_id: 'private-creator-account',
      state: { token: 'private-auth-ticket' },
    };
    await storage.runTransaction(
      'readwrite',
      [INGESTION_STORES.chats, INGESTION_STORES.messages, INGESTION_STORES.credentials],
      async (tx) => {
        await tx.put(INGESTION_STORES.chats, chat);
        await tx.put(INGESTION_STORES.messages, message);
        await tx.put(INGESTION_STORES.credentials, credential);
      },
    );

    const raw = serializedRecords(indexedDb, databaseName);
    assert.match(raw, /ofca-idb-aesgcm\/v1/);
    for (const plaintext of [
      chat.chat_id,
      chat.display_name,
      chat.platform_user_id,
      message.message_id,
      message.text,
      credential.creator_account_id,
      credential.state.token,
      credential.key,
    ]) assert.equal(raw.includes(plaintext), false, `raw IndexedDB leaked ${plaintext}`);

    const restarted = factory(indexedDb, { databaseName, encryptionKey: KEY_A });
    await restarted.runTransaction(
      'readonly',
      [INGESTION_STORES.chats, INGESTION_STORES.messages, INGESTION_STORES.credentials],
      async (tx) => {
        assert.deepEqual(await tx.get(INGESTION_STORES.chats, chat.chat_id), chat);
        assert.deepEqual(
          await tx.getAllKeysFromIndex(INGESTION_STORES.messages, 'chat_id', chat.chat_id),
          [message.message_id],
        );
        assert.deepEqual(await tx.get(INGESTION_STORES.messages, message.message_id), message);
        assert.deepEqual(await tx.get(INGESTION_STORES.credentials, credential.key), credential);
      },
    );
  });
}

test('encrypted records use fresh nonces and fail closed on wrong keys or tampering', async () => {
  const indexedDb = new FakeIndexedDb();
  const databaseName = 'encrypted-authentication-failures';
  const storage = createIndexedDbIngestionStorage(indexedDb, {
    databaseName,
    encryptionKey: KEY_A,
  });
  const record = { chat_id: 'chat-secret', display_name: 'Secret Name' };
  await storage.runTransaction('readwrite', [INGESTION_STORES.chats], (tx) => (
    tx.put(INGESTION_STORES.chats, record)
  ));
  const initialRecords = indexedDb.databases.get(databaseName).stores.get(INGESTION_STORES.chats).records;
  const [rawKey, firstEnvelope] = [...initialRecords.entries()][0];
  await storage.runTransaction('readwrite', [INGESTION_STORES.chats], (tx) => (
    tx.put(INGESTION_STORES.chats, record)
  ));
  const records = indexedDb.databases.get(databaseName).stores.get(INGESTION_STORES.chats).records;
  const secondEnvelope = records.get(rawKey);
  assert.notEqual(firstEnvelope.__ofca_nonce, secondEnvelope.__ofca_nonce);
  assert.notEqual(firstEnvelope.__ofca_ciphertext, secondEnvelope.__ofca_ciphertext);

  const wrongKey = createIndexedDbIngestionStorage(indexedDb, {
    databaseName,
    encryptionKey: KEY_B,
  });
  await assert.rejects(
    wrongKey.runTransaction('readonly', [INGESTION_STORES.chats], (tx) => (
      tx.get(INGESTION_STORES.chats, record.chat_id)
    )),
    /encryption key does not match/,
  );

  secondEnvelope.__ofca_ciphertext = `${secondEnvelope.__ofca_ciphertext.slice(0, -2)}AA`;
  await assert.rejects(
    storage.runTransaction('readonly', [INGESTION_STORES.chats], (tx) => (
      tx.get(INGESTION_STORES.chats, record.chat_id)
    )),
    /could not be authenticated/,
  );
});

test('Full-mode IndexedDB refuses to open without a Brain-unsealed key', () => {
  assert.throws(
    () => createIndexedDbIngestionStorage(new FakeIndexedDb(), { databaseName: 'plaintext' }),
    /encryption key is required/,
  );
  assert.throws(
    () => createReadOnlyIndexedDbIngestionStorage(new FakeIndexedDb(), { databaseName: 'plaintext' }),
    /encryption key is required/,
  );
});
