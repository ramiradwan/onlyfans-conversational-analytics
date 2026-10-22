// Protected release client. The download Worker remains read-only.
import { createHash } from 'node:crypto';
import { open, readFile, lstat, writeFile, rename } from 'node:fs/promises';
import { resolve, join, dirname, parse } from 'node:path';
import { pathToFileURL } from 'node:url';
import { S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';

export const BUCKET = 'dipsy-desktop-releases-prod';
export const ENDPOINT = 'https://a7c496ed19319911a55d16da6287bbbd.eu.r2.cloudflarestorage.com';
export const BASE = 'https://updates.dipsy.fi/desktop/v1/';
export const FEED_KEY = 'windows/x64/stable/ConversationAnalytics.appinstaller';
export const FEED_URI = BASE + FEED_KEY;
const NAME = 'Dipsy.OnlyFansConversationalAnalytics';
const PUBLISHER = 'CN=Dipsy, O=Dipsy, L=London, C=GB';
const FAMILY = NAME + '_bvmm2ppd090g2';
const SHA = /^[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}$/;
const MAX_PACKAGE = 1024 * 1024 * 1024;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
class PublicationError extends Error {}
const requireValue = (ok, code) => { if (!ok) throw new PublicationError(code); };
const status = error => error?.$metadata?.httpStatusCode;
const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function closed(value, names) {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === names.length && Object.keys(value).every(key => names.includes(key)), 'release_members');
}
function sameJson(left, right) {
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') return left === right;
  const keys = Object.keys(left).sort(), other = Object.keys(right).sort();
  return JSON.stringify(keys) === JSON.stringify(other) && keys.every(key => sameJson(left[key], right[key]));
}

function closedJson(bytes) {
  const text = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
  const value = JSON.parse(text), stack = [];
  // JSON.parse establishes grammar first. Inspect original keys before any
  // duplicate could disappear, including escaped spellings of the same key.
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '{' || c === '[') {
      stack.push(c === '{' ? new Set() : null);
      requireValue(stack.length <= 64, 'release_json_depth');
    } else if (c === '}' || c === ']') stack.pop();
    else if (c === '"') {
      const start = i++;
      for (; i < text.length; i++) {
        if (text[i] === '\\') i++;
        else if (text[i] === '"') break;
      }
      let next = i + 1;
      while (/\s/.test(text[next] ?? '') && next < text.length) next++;
      if (text[next] === ':') {
        const key = JSON.parse(text.slice(start, i + 1)), keys = stack.at(-1);
        requireValue(keys !== null && !keys.has(key), 'release_duplicate_key');
        keys.add(key);
      }
    }
  }
  function finite(item) {
    requireValue(typeof item !== 'number' || Number.isFinite(item), 'release_nonfinite');
    if (item && typeof item === 'object') Object.values(item).forEach(finite);
  }
  finite(value);
  return value;
}

export function contextFromEnvironment(env) {
  requireValue(env.GITHUB_ACTIONS === 'true' && env.GITHUB_EVENT_NAME === 'workflow_dispatch'
    && env.GITHUB_REPOSITORY === 'ramiradwan/onlyfans-conversational-analytics', 'unauthorized_execution');
  requireValue(env.GITHUB_REF_TYPE === 'tag' && /^v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$/.test(env.GITHUB_REF_NAME ?? ''), 'unauthorized_ref');
  requireValue(COMMIT.test(env.GITHUB_SHA ?? '') && env.GITHUB_SHA === env.PRODUCT_REVISION, 'source_mismatch');
  requireValue(env.DESKTOP_RELEASE_TARGET === 'msix-direct' && env.DIRECT_PUBLICATION_REQUESTED === 'true', 'publication_not_requested');
  requireValue(/^[1-9][0-9]*$/.test(env.GITHUB_RUN_ID ?? '') && /^[1-9][0-9]*$/.test(env.GITHUB_RUN_ATTEMPT ?? ''), 'invalid_run');
  for (const [key, value] of Object.entries({ DESKTOP_R2_BUCKET: BUCKET, DESKTOP_R2_ENDPOINT: ENDPOINT,
    DESKTOP_UPDATES_BASE_URL: BASE, MSIX_DIRECT_PUBLISHER: PUBLISHER,
    MSIX_DIRECT_EXPECTED_FAMILY: FAMILY, MSIX_DIRECT_APPINSTALLER_URI: FEED_URI })) {
    requireValue(env[key] === value, 'unapproved_delivery_inputs');
  }
  return { sourceCommit: env.PRODUCT_REVISION, runId: env.GITHUB_RUN_ID, runAttempt: env.GITHUB_RUN_ATTEMPT, ref: env.GITHUB_REF_NAME };
}

export function versionSequence(version) {
  requireValue(/^1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.0$/.test(version ?? ''), 'invalid_package_version');
  const [, minor, build] = version.split('.').map(Number);
  requireValue(minor <= 65535 && build <= 65535 && minor * 65536 + build > 0, 'invalid_package_version');
  return minor * 65536 + build;
}

// Accept only the closed XML shape emitted by the existing producer/XmlWriter.
// No general XML interpretation, entities, DTDs, extensions or downgrade flags.
export function descriptorFields(bytes) {
  requireValue(bytes.length > 0 && bytes.length <= 16384, 'descriptor_size');
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  const version = '(1\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.0)';
  const expression = new RegExp('^\\s*(?:<\\?xml version=["\']1\\.0["\'] encoding=["\']utf-8["\']\\?>\\s*)?'
    + '<AppInstaller xmlns="http://schemas.microsoft.com/appx/appinstaller/2018" Version="' + version + '" Uri="' + escape(FEED_URI) + '">\\s*'
    + '<MainPackage Name="' + escape(NAME) + '" Publisher="' + escape(PUBLISHER) + '" Version="\\1" ProcessorArchitecture="x64" Uri="'
    + escape(BASE) + 'packages/([0-9a-f]{64})/ConversationAnalytics_\\1_x64\\.msix"\\s*/>\\s*'
    + '<UpdateSettings>\\s*<OnLaunch HoursBetweenUpdateChecks="6" ShowPrompt="false" UpdateBlocksActivation="false"\\s*/>\\s*'
    + '<AutomaticBackgroundTask\\s*/>\\s*</UpdateSettings>\\s*</AppInstaller>\\s*$');
  const match = expression.exec(text);
  requireValue(Boolean(match), 'descriptor_policy_mismatch');
  return { version: match[1], sequence: versionSequence(match[1]), packageSha256: match[2] };
}

async function regular(path, maximum) {
  for (let parent = resolve(path); parent !== parse(parent).root; parent = dirname(parent)) {
    requireValue(!(await lstat(parent)).isSymbolicLink(), 'release_path_alias');
  }
  const info = await lstat(path);
  requireValue(info.isFile() && info.size > 0 && info.size <= maximum, 'release_file_bound');
  return info;
}
async function small(path, maximum = 32768) {
  await regular(path, maximum);
  const bytes = await readFile(path);
  requireValue(bytes.length > 0 && bytes.length <= maximum, 'release_file_bound');
  return bytes;
}
async function digest(stream, expectedSize, maximum) {
  const h = createHash('sha256');
  let size = 0, prefix = Buffer.alloc(0);
  try {
    for await (const chunk of stream) {
      const bytes = Buffer.from(chunk);
      size += bytes.length;
      requireValue(size <= maximum, 'response_too_large');
      h.update(bytes);
      if (prefix.length < 1024) prefix = Buffer.concat([prefix, bytes.subarray(0, 1024 - prefix.length)]);
    }
  } finally { stream.destroy?.(); }
  requireValue(size === expectedSize, 'byte_count_mismatch');
  return { size, sha256: h.digest('hex'), prefix };
}

export async function loadRelease(directory, admissionPath, verificationPath, deliveryPath, context) {
  const recordBytes = await small(join(directory, 'desktop.artifact-release.json'));
  const record = JSON.parse(recordBytes);
  const admissionBytes = await small(admissionPath);
  const admission = JSON.parse(admissionBytes);
  const verifiedBytes = await small(verificationPath);
  const verified = JSON.parse(verifiedBytes);
  const deliveryBytes = await small(deliveryPath);
  const delivery = JSON.parse(deliveryBytes);
  const v2 = record.schema === 'ofca-desktop-artifact-release/v2';
  if (v2) {
    closedJson(recordBytes); closedJson(admissionBytes); closedJson(verifiedBytes);
    closed(record, ['schema','source_commit','source_tree','target','application_version','package_version',
      'profile_sha256','inventory_sha256','build_sha256','expected_family','verification_sha256',
      'signature_scope','agent_file','agent_sha256','resolved_direct_inputs_sha256','descriptor_sha256','artifact_set']);
    requireValue(record.artifact_set?.kind !== 'related_bundle', 'related_stable_rollout_not_configured');
    closed(record.artifact_set, ['kind','core']);
    closed(record.artifact_set.core, ['file','sha256','size']);
    const core = record.artifact_set.core;
    requireValue(record.artifact_set.kind === 'main_package'
      && core.file === `ConversationAnalytics_${record.package_version}_x64.msix` && SHA.test(core.sha256 ?? '')
      && Number.isSafeInteger(core.size) && core.size > 0 && core.size <= MAX_PACKAGE, 'release_artifact_set');
    closed(admission, ['schema','source_commit','source_tree','artifact_record_sha256','descriptor_sha256',
      'verification_sha256','signature','identity','direct_delivery_sha256','artifact_set']);
    closed(verified, ['schema','purpose','distribution','mode','identity','file_count','package_sha256',
      'profile_sha256','inventory_sha256','signature','artifact_set']);
    closed(verified.identity, ['Name','Publisher','Version','Architecture','FullName','FamilyName']);
    requireValue(['profile_sha256','inventory_sha256','build_sha256','verification_sha256','agent_sha256',
      'resolved_direct_inputs_sha256'].every(key => SHA.test(record[key] ?? ''))
      && record.verification_sha256 === hash(verifiedBytes)
      && record.profile_sha256 === verified.profile_sha256 && record.inventory_sha256 === verified.inventory_sha256
      && sameJson(admission.identity, verified.identity), 'v2_verification_binding');
    requireValue(sameJson(admission.artifact_set, record.artifact_set)
      && sameJson(verified.artifact_set, record.artifact_set), 'artifact_set_verification_mismatch');
    // Derived aliases do not change the serialized versioned records.
    record.package_file = core.file; record.package_sha256 = core.sha256;
    admission.package_sha256 = core.sha256;
  }
  requireValue(record.schema === (v2 ? 'ofca-desktop-artifact-release/v2' : 'ofca-desktop-artifact-release/v1') && record.target === 'msix-direct'
    && record.signature_scope === 'trusted-timestamped' && record.expected_family === FAMILY, 'release_scope');
  requireValue(record.source_commit === context.sourceCommit && COMMIT.test(record.source_tree ?? ''), 'release_source');
  requireValue(admission.schema === (v2 ? 'ofca-direct-publication-admission/v2' : 'ofca-direct-publication-admission/v1') && admission.source_commit === context.sourceCommit
    && admission.source_tree === record.source_tree && admission.artifact_record_sha256 === hash(recordBytes)
    && admission.signature === 'trusted-timestamped' && admission.verification_sha256 === hash(verifiedBytes)
    && admission.direct_delivery_sha256 === hash(deliveryBytes), 'independent_admission_mismatch');
  requireValue(delivery.hosting?.bucket === BUCKET && delivery.hosting.s3_endpoint === ENDPOINT
    && delivery.hosting.app_installer_uri === FEED_URI && delivery.signing?.subject === PUBLISHER
    && delivery.expected_package_family === FAMILY, 'delivery_source_mismatch');
  const identity = verified.identity;
  requireValue(verified.schema === (v2 ? 'ofca-msix-release-verification/v2' : 'ofca-msix-release-verification/v1') && verified.mode === 'DirectSigned'
    && verified.purpose === 'production' && verified.distribution === 'direct-appinstaller'
    && verified.signature === 'trusted-timestamped' && identity?.Name === NAME && identity.Publisher === PUBLISHER
    && identity.FamilyName === FAMILY && identity.Architecture === 'x64' && identity.Version === record.package_version,
  'final_signature_scope');
  versionSequence(record.package_version);
  requireValue(record.package_file === `ConversationAnalytics_${record.package_version}_x64.msix`
    && SHA.test(record.package_sha256 ?? '') && SHA.test(record.descriptor_sha256 ?? '')
    && verified.package_sha256 === record.package_sha256 && admission.package_sha256 === record.package_sha256
    && admission.descriptor_sha256 === record.descriptor_sha256, 'release_hash_mismatch');
  const descriptor = await small(join(directory, 'ConversationAnalytics.appinstaller'), 16384);
  const fields = descriptorFields(descriptor);
  requireValue(hash(descriptor) === record.descriptor_sha256 && fields.version === record.package_version
    && fields.packageSha256 === record.package_sha256, 'descriptor_release_mismatch');
  const packagePath = join(directory, record.package_file);
  const info = await regular(packagePath, MAX_PACKAGE);
  if (v2) requireValue(info.size === record.artifact_set.core.size, 'artifact_size_mismatch');
  const file = await open(packagePath, 'r');
  try {
    const observed = await digest(file.createReadStream({ autoClose: false }), info.size, MAX_PACKAGE);
    requireValue(observed.sha256 === record.package_sha256, 'package_changed_after_verification');
    return { record, descriptor, sequence: fields.sequence, file, ...observed,
      key: `packages/${observed.sha256}/${record.package_file}` };
  } catch (error) { await file.close(); throw error; }
}

async function send(client, command) { return client.send(command, { abortSignal: AbortSignal.timeout(120000) }); }
async function storedFeed(client) {
  let response;
  try { response = await send(client, new GetObjectCommand({ Bucket: BUCKET, Key: FEED_KEY })); }
  catch (error) { if (status(error) === 404) return null; throw error; }
  try {
    requireValue(response.ContentLength > 0 && response.ContentLength <= 16384 && /^"[0-9a-f]{32}"$/.test(response.ETag ?? ''), 'stored_descriptor_invalid');
    const chunks = []; let size = 0;
    for await (const chunk of response.Body) {
      size += chunk.length; requireValue(size <= 16384, 'descriptor_size'); chunks.push(chunk);
    }
    requireValue(size === response.ContentLength, 'byte_count_mismatch');
    const bytes = Buffer.concat(chunks);
    return { bytes, etag: response.ETag, ...descriptorFields(bytes) };
  } finally { response.Body?.destroy?.(); }
}

function headers(response, uri, type, length, code = 200) {
  requireValue(response.status === code && response.url === uri, 'served_status_or_uri');
  requireValue(response.headers.get('content-type') === type && response.headers.get('content-length') === String(length)
    && [null, 'identity'].includes(response.headers.get('content-encoding'))
    && response.headers.get('accept-ranges') === 'bytes'
    && response.headers.get('cache-control')?.includes('no-transform'), 'served_headers');
  const caching = response.headers.get('cache-control').split(',').map(value => value.trim()).sort();
  const expected = (type === 'application/appinstaller' ? ['no-store','no-transform']
    : ['public','max-age=31536000','immutable','no-transform']).sort();
  requireValue(JSON.stringify(caching) === JSON.stringify(expected), 'served_cache_policy');
}
async function served(fetcher, uri, type, size, expectedHash) {
  const options = { redirect: 'error', headers: { 'Accept-Encoding': 'identity' }, signal: AbortSignal.timeout(120000) };
  const head = await fetcher(uri, { ...options, method: 'HEAD' });
  headers(head, uri, type, size);
  const response = await fetcher(uri, options);
  try {
    headers(response, uri, type, size);
    requireValue(Boolean(response.body), 'served_body_missing');
    const actual = await digest(response.body, size, size);
    requireValue(actual.sha256 === expectedHash, 'served_hash_mismatch');
  } finally { if (response.body && !response.body.locked) await response.body.cancel().catch(() => {}); }
}

export async function publish(client, release, context, fetcher = fetch, event = async () => {}) {
  const previous = await storedFeed(client);
  if (previous) {
    requireValue(previous.sequence <= release.sequence, 'descriptor_downgrade_refused');
    requireValue(previous.sequence !== release.sequence || hash(previous.bytes) === hash(release.descriptor), 'same_version_replacement_refused');
  }
  const receipt = { schema: 'ofca-direct-publication/v1', ...context, packageKey: release.key,
    packageSha256: release.sha256, packageBytes: release.size, descriptorSha256: hash(release.descriptor),
    descriptorUri: FEED_URI, previousEtag: previous?.etag ?? null, stages: [], success: false };
  await event(receipt); // Durable intent precedes either write.
  try {
    return await finishPublication(client, release, context, fetcher, event, previous, receipt);
  } catch (error) {
    receipt.success = false;
    receipt.failure = { code: error instanceof PublicationError ? error.message : 'publication_failed', httpStatus: status(error) ?? null };
    try { await event(receipt); } catch { /* Preserve the request/evidence failure. */ }
    throw error;
  }
}

async function finishPublication(client, release, context, fetcher, event, previous, receipt) {
  const uploadStream = release.file.createReadStream({ start: 0, autoClose: false });
  try {
    await send(client, new PutObjectCommand({ Bucket: BUCKET, Key: release.key,
      Body: uploadStream, ContentLength: release.size,
      ContentType: 'application/msix', CacheControl: 'public, max-age=31536000, immutable, no-transform',
      Metadata: { 'release-sha256': release.sha256, 'release-source': context.sourceCommit }, IfNoneMatch: '*' }));
    receipt.stages.push('package_uploaded');
  } catch (error) {
    if (status(error) !== 412) throw error;
    const existing = await send(client, new HeadObjectCommand({ Bucket: BUCKET, Key: release.key }));
    requireValue(existing.ContentLength === release.size && existing.Metadata?.['release-sha256'] === release.sha256, 'immutable_object_conflict');
    // Reuse requires a real read/hash, not metadata alone.
    const existingBytes = await send(client, new GetObjectCommand({ Bucket: BUCKET, Key: release.key }));
    try {
      requireValue(existingBytes.ContentLength === release.size, 'immutable_object_conflict');
      const actual = await digest(existingBytes.Body, release.size, release.size);
      requireValue(actual.sha256 === release.sha256, 'immutable_object_conflict');
    } finally { existingBytes.Body?.destroy?.(); }
    receipt.stages.push('identical_package_reused');
  } finally { uploadStream.destroy(); }
  await served(fetcher, BASE + release.key, 'application/msix', release.size, release.sha256);
  const rangeUri = BASE + release.key, rangeLength = Math.min(1024, release.size);
  const range = await fetcher(rangeUri, { redirect: 'error', signal: AbortSignal.timeout(120000),
    headers: { Range: `bytes=0-${rangeLength - 1}`, 'Accept-Encoding': 'identity' } });
  try {
    headers(range, rangeUri, 'application/msix', rangeLength, 206);
    requireValue(range.headers.get('content-range') === `bytes 0-${rangeLength - 1}/${release.size}`, 'served_range_headers');
    const actual = await digest(range.body, rangeLength, rangeLength);
    requireValue(actual.sha256 === hash(release.prefix), 'served_range_bytes');
  } finally { if (range.body && !range.body.locked) await range.body.cancel().catch(() => {}); }
  receipt.stages.push('served_package_verified'); await event(receipt);
  if (!previous || hash(previous.bytes) !== hash(release.descriptor)) {
    await send(client, new PutObjectCommand({ Bucket: BUCKET, Key: FEED_KEY, Body: release.descriptor,
      ContentLength: release.descriptor.length, ContentType: 'application/appinstaller', CacheControl: 'no-store, no-transform',
      Metadata: { 'release-sha256': hash(release.descriptor), 'release-source': context.sourceCommit },
      ...(previous ? { IfMatch: previous.etag } : { IfNoneMatch: '*' }) }));
  }
  receipt.stages.push('descriptor_promoted'); await event(receipt);
  await served(fetcher, FEED_URI, 'application/appinstaller', release.descriptor.length, hash(release.descriptor));
  receipt.stages.push('served_descriptor_verified'); receipt.success = true;
  await event(receipt);
  return receipt;
}

async function main() {
  const context = contextFromEnvironment(process.env);
  requireValue(process.argv.length === 7, 'invalid_arguments');
  const [directory, admission, verification, delivery, receiptPath] = process.argv.slice(2);
  requireValue(process.env.DESKTOP_R2_ACCESS_KEY_ID && process.env.DESKTOP_R2_SECRET_ACCESS_KEY, 'missing_release_credentials');
  const release = await loadRelease(resolve(directory), resolve(admission), resolve(verification), resolve(delivery), context);
  const client = new S3Client({ region: 'auto', endpoint: ENDPOINT, maxAttempts: 1,
    requestChecksumCalculation: 'WHEN_REQUIRED', responseChecksumValidation: 'WHEN_REQUIRED',
    credentials: { accessKeyId: process.env.DESKTOP_R2_ACCESS_KEY_ID, secretAccessKey: process.env.DESKTOP_R2_SECRET_ACCESS_KEY } });
  let started = false;
  const event = async receipt => {
    const bytes = JSON.stringify(receipt, null, 2) + '\n';
    if (!started) { await writeFile(receiptPath, bytes, { flag: 'wx' }); started = true; }
    else { await writeFile(receiptPath + '.next', bytes, { flag: 'wx' }); await rename(receiptPath + '.next', receiptPath); }
  };
  try {
    const receipt = await publish(client, release, context, fetch, event);
    console.log(JSON.stringify(receipt));
  } finally { await release.file.close(); client.destroy(); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(JSON.stringify({ success: false, code: error instanceof PublicationError ? error.message : 'publication_failed', httpStatus: status(error) ?? null }));
    process.exitCode = 1;
  });
}
