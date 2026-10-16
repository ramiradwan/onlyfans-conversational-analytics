import { randomBytes, createHash } from 'node:crypto';
import { mkdir, open, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { S3Client, PutObjectCommand, HeadObjectCommand, GetObjectCommand, DeleteObjectCommand } from '@aws-sdk/client-s3';

export const BUCKET = 'dipsy-desktop-releases-prod';
export const ENDPOINT = 'https://a7c496ed19319911a55d16da6287bbbd.eu.r2.cloudflarestorage.com';
const REPOSITORY = 'ramiradwan/onlyfans-conversational-analytics';
const SIZE = 4096;
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const status = error => error?.$metadata?.httpStatusCode;
class CheckError extends Error {}
function requireCheck(condition, code) { if (!condition) throw new CheckError(code); }
function safeFailure(error) {
  return { code: error instanceof CheckError ? error.message : 'request_failed', httpStatus: status(error) ?? null };
}

export function contextFromEnvironment(env) {
  requireCheck(env.GITHUB_ACTIONS === 'true' && env.GITHUB_EVENT_NAME === 'workflow_dispatch' && env.GITHUB_REPOSITORY === REPOSITORY, 'unauthorized_execution');
  requireCheck(env.GITHUB_REF_TYPE === 'tag' && /^vcredential-check-[0-9A-Za-z][0-9A-Za-z._-]*$/.test(env.GITHUB_REF_NAME ?? ''), 'unauthorized_ref');
  requireCheck(/^[0-9a-f]{40}$/.test(env.GITHUB_SHA ?? '') && env.GITHUB_SHA === env.PRODUCT_REVISION, 'source_mismatch');
  requireCheck(/^[1-9][0-9]*$/.test(env.GITHUB_RUN_ID ?? '') && /^[1-9][0-9]*$/.test(env.GITHUB_RUN_ATTEMPT ?? ''), 'invalid_run');
  requireCheck(env.DESKTOP_R2_BUCKET === BUCKET && env.DESKTOP_R2_ENDPOINT === ENDPOINT, 'unapproved_storage');
  requireCheck(Boolean(env.DESKTOP_R2_ACCESS_KEY_ID) && Boolean(env.DESKTOP_R2_SECRET_ACCESS_KEY), 'missing_release_credentials');
  return { sourceCommit: env.GITHUB_SHA, runId: env.GITHUB_RUN_ID, runAttempt: env.GITHUB_RUN_ATTEMPT, ref: env.GITHUB_REF_NAME };
}

export function makeIntent(context, body, nonce = randomBytes(32).toString('hex')) {
  requireCheck(body.length === SIZE && /^[0-9a-f]{64}$/.test(nonce), 'invalid_synthetic_data');
  return { ...context, bucket: BUCKET, endpoint: ENDPOINT, nonce, key: `_credential-checks/github-release/${context.runId}/${nonce}.bin`, size: SIZE, sha256: sha256(body) };
}

export function validateIntent(intent, context) {
  requireCheck(intent.bucket === BUCKET && intent.endpoint === ENDPOINT && intent.size === SIZE && /^[0-9a-f]{64}$/.test(intent.sha256 ?? '') && /^[0-9a-f]{64}$/.test(intent.nonce ?? ''), 'invalid_intent');
  for (const field of ['sourceCommit', 'runId', 'runAttempt', 'ref']) requireCheck(intent[field] === context[field], 'intent_owner_mismatch');
  requireCheck(intent.key === `_credential-checks/github-release/${context.runId}/${intent.nonce}.bin`, 'unsafe_object_key');
}

function metadata(intent) {
  return { 'check-nonce': intent.nonce, 'check-run-id': intent.runId, 'check-run-attempt': intent.runAttempt, 'check-source': intent.sourceCommit, 'check-sha256': intent.sha256 };
}
function owned(response, intent) {
  requireCheck(response.ContentLength === SIZE, 'object_size_mismatch');
  for (const [key, value] of Object.entries(metadata(intent))) requireCheck(response.Metadata?.[key] === value, 'object_owner_mismatch');
}
async function send(client, command) {
  return client.send(command, { abortSignal: AbortSignal.timeout(20000) });
}
async function head(client, intent) {
  try { return await send(client, new HeadObjectCommand({ Bucket: BUCKET, Key: intent.key })); }
  catch (error) { if (status(error) === 404) return null; throw error; }
}
async function bytesFrom(response) {
  requireCheck(Boolean(response.Body), 'missing_response_body');
  const chunks = [];
  let size = 0;
  try {
    for await (const chunk of response.Body) {
      size += chunk.length;
      requireCheck(size <= SIZE, 'response_too_large');
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  } finally { response.Body.destroy?.(); }
}

export async function requireAbsent(client, intent) {
  const failures = [];
  const result = {};
  // Run both probes even when one fails. A 403 is never treated as absence.
  for (const [name, Command] of [['headStatus', HeadObjectCommand], ['getStatus', GetObjectCommand]]) {
    try {
      const response = await send(client, new Command({ Bucket: BUCKET, Key: intent.key }));
      response.Body?.destroy?.();
      result[name] = response.$metadata?.httpStatusCode ?? 200;
      failures.push(new CheckError('object_still_exists'));
    } catch (error) {
      result[name] = status(error) ?? null;
      if (status(error) !== 404) failures.push(error);
    }
  }
  requireCheck(failures.length === 0, 'absence_not_confirmed');
  return result;
}

export async function cleanup(client, intent, context) {
  validateIntent(intent, context);
  const existing = await head(client, intent);
  let deleted = false;
  if (existing) {
    owned(existing, intent);
    const response = await send(client, new GetObjectCommand({ Bucket: BUCKET, Key: intent.key, IfMatch: existing.ETag }));
    let body;
    try { owned(response, intent); body = await bytesFrom(response); }
    finally { response.Body?.destroy?.(); }
    requireCheck(body.length === SIZE && sha256(body) === intent.sha256, 'cleanup_content_mismatch');
    // R2 has no conditional DeleteObject. Only this random, verified owned key
    // is deleted; there is no listing, prefix deletion or bulk operation.
    const deletion = await send(client, new DeleteObjectCommand({ Bucket: BUCKET, Key: intent.key }));
    requireCheck(deletion.$metadata?.httpStatusCode === 204, 'delete_not_confirmed');
    deleted = true;
  }
  return { deleted, ...(await requireAbsent(client, intent)) };
}

export async function runCheck(client, intent, body, context) {
  validateIntent(intent, context);
  const receipt = { ...intent, runUrl: `https://github.com/${REPOSITORY}/actions/runs/${context.runId}`, success: false };
  try {
    requireCheck(await head(client, intent) === null, 'refusing_overwrite');
    const uploaded = await send(client, new PutObjectCommand({ Bucket: BUCKET, Key: intent.key, Body: body, ContentLength: SIZE, ContentType: 'application/octet-stream', CacheControl: 'no-store', Metadata: metadata(intent), IfNoneMatch: '*' }));
    requireCheck(uploaded.$metadata?.httpStatusCode === 200, 'upload_not_confirmed');
    receipt.upload = { httpStatus: 200, bytes: body.length, sha256: sha256(body), createOnly: true };
    const response = await send(client, new GetObjectCommand({ Bucket: BUCKET, Key: intent.key }));
    let read;
    try { owned(response, intent); read = await bytesFrom(response); }
    finally { response.Body?.destroy?.(); }
    receipt.read = { httpStatus: response.$metadata?.httpStatusCode, bytes: read.length, sha256: sha256(read) };
    requireCheck(receipt.read.httpStatus === 200 && read.length === SIZE && receipt.read.sha256 === intent.sha256, 'read_verification_failed');
  } catch (error) { receipt.verificationFailure = safeFailure(error); }
  finally {
    try { receipt.cleanup = await cleanup(client, intent, context); }
    catch (error) { receipt.cleanupFailure = safeFailure(error); }
  }
  receipt.success = Boolean(receipt.upload && receipt.read && !receipt.verificationFailure && receipt.cleanup && !receipt.cleanupFailure);
  return receipt;
}

async function saveExclusive(path, value) {
  const file = await open(path, 'wx', 0o600);
  try { await file.writeFile(JSON.stringify(value, null, 2)); await file.sync(); }
  finally { await file.close(); }
}

async function main() {
  const [mode, directory, ...extra] = process.argv.slice(2);
  requireCheck(['check', 'cleanup'].includes(mode) && Boolean(directory) && extra.length === 0, 'invalid_arguments');
  const context = contextFromEnvironment(process.env);
  const root = resolve(directory);
  await mkdir(root, { recursive: true });
  const intentPath = join(root, 'intent.json');
  const client = new S3Client({ region: 'auto', endpoint: ENDPOINT, forcePathStyle: true, maxAttempts: 1, requestChecksumCalculation: 'WHEN_REQUIRED', responseChecksumValidation: 'WHEN_REQUIRED', credentials: { accessKeyId: process.env.DESKTOP_R2_ACCESS_KEY_ID, secretAccessKey: process.env.DESKTOP_R2_SECRET_ACCESS_KEY } });
  try {
    if (mode === 'check') {
      const body = randomBytes(SIZE);
      const intent = makeIntent(context, body);
      // Durable local ownership evidence precedes the first network request.
      await saveExclusive(intentPath, intent);
      const receipt = await runCheck(client, intent, body, context);
      await writeFile(join(root, 'receipt.json'), JSON.stringify(receipt, null, 2));
      console.log(JSON.stringify(receipt));
      if (!receipt.success) process.exitCode = 1;
    } else {
      let intent;
      try { intent = JSON.parse(await readFile(intentPath, 'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      const receipt = intent ? { ...context, key: intent.key, ...(await cleanup(client, intent, context)), success: true } : { ...context, success: true, state: 'no_upload_intent' };
      await writeFile(join(root, 'cleanup-backup.json'), JSON.stringify(receipt, null, 2));
      console.log(JSON.stringify(receipt));
    }
  } finally { client.destroy(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(JSON.stringify(safeFailure(error))); process.exitCode = 1; });
}
