import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { S3Client, PutObjectCommand, HeadObjectCommand, GetObjectCommand, DeleteObjectCommand } from '@aws-sdk/client-s3';
import { BUCKET, ENDPOINT, contextFromEnvironment, makeIntent, runCheck, cleanup, requireAbsent } from './check.mjs';

const context = { runId: '123', runAttempt: '1', sourceCommit: 'a'.repeat(40), ref: 'vcredential-check-local' };
const body = Buffer.alloc(4096, 83);
const intent = makeIntent(context, body, 'b'.repeat(64));
function failure(code) { return Object.assign(new Error('synthetic request failure'), { $metadata: { httpStatusCode: code } }); }
class Storage {
  calls = [];
  object = null;
  async send(command) {
    this.calls.push(command);
    assert.equal(command.input.Bucket, BUCKET);
    assert.equal(command.input.Key, intent.key);
    if (command instanceof PutObjectCommand) {
      assert.equal(command.input.IfNoneMatch, '*');
      if (this.object) throw failure(412);
      this.object = { bytes: Buffer.from(command.input.Body), Metadata: command.input.Metadata, ETag: '"synthetic-etag"' };
      return { $metadata: { httpStatusCode: 200 } };
    }
    if (command instanceof DeleteObjectCommand) {
      this.object = null;
      return { $metadata: { httpStatusCode: 204 } };
    }
    if (!this.object) throw failure(404);
    const response = { ContentLength: this.object.bytes.length, Metadata: this.object.Metadata, ETag: this.object.ETag, $metadata: { httpStatusCode: 200 } };
    if (command instanceof GetObjectCommand) response.Body = Readable.from([this.object.bytes]);
    else assert.ok(command instanceof HeadObjectCommand);
    return response;
  }
}

test('4096 bytes round trip with matching SHA-256 and both deletion probes', async () => {
  const storage = new Storage();
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, true);
  assert.deepEqual(receipt.upload, { bytes: 4096, sha256: intent.sha256, httpStatus: 200, createOnly: true });
  assert.deepEqual(receipt.read, { bytes: 4096, sha256: intent.sha256, httpStatus: 200 });
  assert.deepEqual(receipt.cleanup, { deleted: true, headStatus: 404, getStatus: 404 });
  assert.equal(storage.object, null);
});

test('preexisting object is neither overwritten nor deleted', async () => {
  const storage = new Storage();
  storage.object = { bytes: body, Metadata: {}, ETag: '"foreign"' };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.verificationFailure.code, 'refusing_overwrite');
  assert.equal(receipt.cleanupFailure.code, 'object_owner_mismatch');
  assert.ok(storage.calls.every(command => !(command instanceof PutObjectCommand) && !(command instanceof DeleteObjectCommand)));
});

test('atomic creation refuses an object appearing after the initial HEAD', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => {
    if (command instanceof PutObjectCommand) storage.object = { bytes: body, Metadata: {}, ETag: '"foreign"' };
    return send(command);
  };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.verificationFailure.httpStatus, 412);
  assert.equal(receipt.success, false);
  assert.ok(storage.object);
  assert.ok(storage.calls.every(command => !(command instanceof DeleteObjectCommand)));
});

test('lost PUT response still cleans up the verified owned object', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => { const response = await send(command); if (command instanceof PutObjectCommand) throw failure(503); return response; };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.verificationFailure.httpStatus, 503);
  assert.equal(receipt.cleanup.deleted, true);
  assert.equal(storage.object, null);
});

test('failed read fails verification and still attempts cleanup', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  let failed = false;
  storage.send = async command => { if (command instanceof GetObjectCommand && storage.object && !failed) { failed = true; throw failure(503); } return send(command); };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.cleanup.deleted, true);
});

test('altered bytes fail SHA verification and unsafe cleanup', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => { const response = await send(command); if (command instanceof PutObjectCommand) storage.object.bytes[0] ^= 1; return response; };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.verificationFailure.code, 'read_verification_failed');
  assert.equal(receipt.cleanupFailure.code, 'cleanup_content_mismatch');
  assert.ok(storage.calls.every(command => !(command instanceof DeleteObjectCommand)));
});

test('failed deletion cannot produce success; backup can recover it', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => { if (command instanceof DeleteObjectCommand) throw failure(503); return send(command); };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.cleanupFailure.httpStatus, 503);
  storage.send = send;
  assert.deepEqual(await cleanup(storage, intent, context), { deleted: true, headStatus: 404, getStatus: 404 });
});

test('backup cleanup is idempotent but repeats both absence probes', async () => {
  const storage = new Storage();
  await runCheck(storage, intent, body, context);
  assert.deepEqual(await cleanup(storage, intent, context), { deleted: false, headStatus: 404, getStatus: 404 });
});

test('HEAD failure does not suppress GET; 403 does not prove absence', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => { if (command instanceof HeadObjectCommand) { storage.calls.push(command); throw failure(403); } return send(command); };
  await assert.rejects(requireAbsent(storage, intent), /absence_not_confirmed/);
  assert.equal(storage.calls.length, 2);
  assert.ok(storage.calls[1] instanceof GetObjectCommand);
});

test('GET failure after deletion fails the check even when HEAD is 404', async () => {
  const storage = new Storage();
  const send = storage.send.bind(storage);
  storage.send = async command => { if (command instanceof GetObjectCommand && !storage.object) throw failure(403); return send(command); };
  const receipt = await runCheck(storage, intent, body, context);
  assert.equal(receipt.success, false);
  assert.equal(receipt.cleanupFailure.code, 'absence_not_confirmed');
});

test('changed nonce, run, source, bucket and public key fail before requests', async () => {
  for (const changed of [{ bucket: 'other' }, { endpoint: 'https://other.example' }, { key: 'windows/x64/stable/ConversationAnalytics.appinstaller' }, { runId: '124' }, { runAttempt: '2' }, { sourceCommit: 'c'.repeat(40) }, { nonce: 'd'.repeat(64) }]) {
    const storage = new Storage();
    await assert.rejects(cleanup(storage, { ...intent, ...changed }, context));
    assert.equal(storage.calls.length, 0);
  }
});

test('only the designated dispatch tag and explicit release inputs are admitted', () => {
  const env = { GITHUB_ACTIONS: 'true', GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REPOSITORY: 'ramiradwan/onlyfans-conversational-analytics', GITHUB_REF_TYPE: 'tag', GITHUB_REF_NAME: context.ref, GITHUB_SHA: context.sourceCommit, PRODUCT_REVISION: context.sourceCommit, GITHUB_RUN_ID: '123', GITHUB_RUN_ATTEMPT: '1', DESKTOP_R2_BUCKET: BUCKET, DESKTOP_R2_ENDPOINT: ENDPOINT, DESKTOP_R2_ACCESS_KEY_ID: 'synthetic-key', DESKTOP_R2_SECRET_ACCESS_KEY: 'synthetic-secret' };
  assert.deepEqual(contextFromEnvironment(env), context);
  for (const changed of [{ GITHUB_REF_TYPE: 'branch' }, { GITHUB_REF_NAME: 'v1.2.3' }, { GITHUB_REPOSITORY: 'other/repo' }, { PRODUCT_REVISION: 'c'.repeat(40) }, { DESKTOP_R2_BUCKET: 'dipsy-desktop-qualification' }, { DESKTOP_R2_ENDPOINT: 'https://other.example' }, { DESKTOP_R2_SECRET_ACCESS_KEY: '' }]) assert.throws(() => contextFromEnvironment({ ...env, ...changed }));
});

test('pinned SDK serializes atomic create-only header and exact object path', async () => {
  const client = new S3Client({ region: 'auto', endpoint: ENDPOINT, forcePathStyle: true, maxAttempts: 1, requestChecksumCalculation: 'WHEN_REQUIRED', credentials: { accessKeyId: 'synthetic-key', secretAccessKey: 'synthetic-secret' }, requestHandler: { handle: async request => {
    assert.equal(request.headers['if-none-match'], '*');
    assert.equal(request.path, `/${BUCKET}/${intent.key}`);
    return { response: { statusCode: 200, headers: {}, body: Readable.from([]) } };
  } } });
  try { await client.send(new PutObjectCommand({ Bucket: BUCKET, Key: intent.key, Body: body, IfNoneMatch: '*' })); }
  finally { client.destroy(); }
});
