/**
 * Authenticated retrieval of one document held in a private repository.
 *
 * A document reaches a release one of two ways, and both are here because the
 * two private repositories keep their documents differently. A document that is
 * committed is fetched at a declared commit revision. A document that is never
 * committed is published as a release asset and fetched by the immutable
 * identifier the API assigns it.
 *
 * This is the single retrieval path a release build uses. The Legal release
 * bindings gate and the packaged signing rule gate are verifiers built on it:
 * each declares the coordinates and credentials it reads and what makes a
 * fetched document acceptable, and neither opens a connection of its own.
 *
 * Every coordinate and credential arrives in the environment rather than on the
 * command line. Nothing here writes a document; staging is the caller's, taken
 * only once its own checks hold, so a refusal leaves the packaging step no
 * input to read.
 *
 * The module depends on nothing outside the Node standard library, so a gate
 * built on it runs before any dependency install.
 */

import { createSign } from 'node:crypto';
import path from 'node:path';
import process from 'node:process';

export const EXIT_COORDINATE_REJECTED = 2;
export const EXIT_CREDENTIAL_ABSENT = 3;
export const EXIT_RETRIEVAL_FAILED = 4;
export const EXIT_NOT_CANONICAL = 5;
export const EXIT_DIGEST_MISMATCH = 6;
export const EXIT_SOURCE_REVISION_MISMATCH = 7;
export const EXIT_SOURCE_REVISION_ABSENT = 8;

export const COMMIT_REVISION = /^[0-9a-f]{40}$/;
const CANONICAL_DIGEST = /^[0-9a-f]{64}$/;
const REPOSITORY = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;
const NUMERIC_IDENTIFIER = /^[0-9]+$/;
const RELEASE_TAG = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/;
const RELEASE_TAG_LIMIT = 255;
const PUBLIC_API_BASE_URL = 'https://api.github.com';
const API_VERSION = '2022-11-28';
const USER_AGENT = 'release-retrieval-gate';
const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', 'localhost']);

/** A refusal carrying the exit code that names which check refused. */
export class GateRefusal extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'GateRefusal';
    this.code = code;
  }
}

export function refuse(code, message) {
  throw new GateRefusal(code, message);
}

export function environmentValue(environment, name) {
  const value = environment[name];
  return typeof value === 'string' ? value.trim() : '';
}

export function argumentValue(argv, name) {
  const prefix = `${name}=`;
  const inline = argv.find((argument) => argument.startsWith(prefix));
  if (inline !== undefined) return inline.slice(prefix.length).trim();
  const index = argv.indexOf(name);
  if (index === -1) return '';
  const next = argv[index + 1];
  return typeof next === 'string' ? next.trim() : '';
}

/**
 * Resolve the API origin. The public origin is the only non-loopback value a
 * gate accepts, so the override is a test seam and cannot redirect a release to
 * a third party.
 */
export function resolveApiBaseUrl(environment, name) {
  const declared = environmentValue(environment, name);
  if (declared === '') return PUBLIC_API_BASE_URL;
  let parsed;
  try {
    parsed = new URL(declared);
  } catch {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is not a URL`);
  }
  if (!LOOPBACK_HOSTS.has(parsed.hostname.replace(/^\[|\]$/g, ''))) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} may only name a loopback address`);
  }
  return parsed.origin;
}

/** A repository path: relative, JSON, and free of traversal or separators. */
export function validateDocumentPath(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (value.startsWith('/') || value.includes('\\') || value.includes('//')) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} must be a relative repository path`);
  }
  const segments = value.split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} must not contain an empty or relative segment`);
  }
  if (!value.endsWith('.json')) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} must name a JSON document`);
  }
  return value;
}

export function validateRevision(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (!COMMIT_REVISION.test(value)) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      `${name} must be a full 40-character lowercase commit revision; `
      + 'a branch name, tag or abbreviated revision is not a fixed coordinate',
    );
  }
  return value;
}

export function validateExpectedDigest(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (value.startsWith('sha256:')) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      `${name} is bare lowercase 64-hex, not the sha256:-prefixed form`,
    );
  }
  if (!CANONICAL_DIGEST.test(value)) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} must be bare lowercase 64-hex`);
  }
  return value;
}

/**
 * A release tag. Unlike a commit revision this is a locator and not an
 * identity: a tag can be repointed at another release after a coordinate is
 * declared. A gate that reads one pairs it with the immutable identifier of the
 * asset it expects and refuses unless the release the tag resolves to publishes
 * exactly that asset, so a moved tag names a release that does not carry the
 * asset rather than substituting a different document.
 */
export function validateReleaseTag(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (value.length > RELEASE_TAG_LIMIT) {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is longer than a release tag can be`);
  }
  if (!RELEASE_TAG.test(value) || value.includes('..')) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      `${name} must be a release tag: an alphanumeric first character followed `
      + 'by letters, digits and . _ - / with no relative segment',
    );
  }
  return value;
}

/**
 * A release asset identifier. The API assigns it, never reuses it, and never
 * moves it to another asset, so it is the fixed coordinate in a pair whose tag
 * is not.
 */
export function validateAssetIdentifier(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (!NUMERIC_IDENTIFIER.test(value)) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      `${name} must be the numeric identifier of one release asset; `
      + 'an asset name is not a fixed coordinate',
    );
  }
  return value;
}

/**
 * Read the retrieval credentials. They are absent until the retrieval
 * application is installed, and absence stops the release here rather than
 * degrading to an unauthenticated or a skipped fetch. ``variables`` names the
 * four environment variables in the order repository, application, private key,
 * installation, so a refusal says which one to supply without printing a value.
 */
export function resolveCredentials(environment, { variables, subject, object }) {
  const [repositoryName, applicationName, privateKeyName, installationName] = variables;
  const absent = variables.filter((name) => environmentValue(environment, name) === '');
  if (absent.length > 0) {
    refuse(
      EXIT_CREDENTIAL_ABSENT,
      `the ${subject} retrieval credential is not configured, so ${object} `
      + `cannot be verified: ${absent.join(', ')}`,
    );
  }
  const repository = environmentValue(environment, repositoryName);
  const applicationId = environmentValue(environment, applicationName);
  const installationId = environmentValue(environment, installationName);
  if (!REPOSITORY.test(repository)) {
    refuse(EXIT_CREDENTIAL_ABSENT, `${repositoryName} must be owner/name`);
  }
  for (const [name, value] of [
    [applicationName, applicationId],
    [installationName, installationId],
  ]) {
    if (!NUMERIC_IDENTIFIER.test(value)) {
      refuse(EXIT_CREDENTIAL_ABSENT, `${name} must be a numeric identifier`);
    }
  }
  let privateKey;
  try {
    privateKey = Buffer.from(environmentValue(environment, privateKeyName), 'base64')
      .toString('utf8');
  } catch {
    privateKey = '';
  }
  if (!privateKey.includes('PRIVATE KEY')) {
    refuse(
      EXIT_CREDENTIAL_ABSENT,
      `${privateKeyName} must be a base64-encoded PEM private key`,
    );
  }
  return Object.freeze({ repository, applicationId, installationId, privateKey });
}

function base64Url(value) {
  return Buffer.from(value).toString('base64url');
}

/** Mint the short-lived application assertion the token exchange requires. */
function applicationAssertion({ applicationId, privateKey }, now = Math.floor(Date.now() / 1000)) {
  const header = base64Url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64Url(
    JSON.stringify({ iat: now - 60, exp: now + 480, iss: applicationId }),
  );
  const signer = createSign('RSA-SHA256');
  signer.update(`${header}.${payload}`);
  signer.end();
  return `${header}.${payload}.${signer.sign(privateKey, 'base64url')}`;
}

/** Hide a runtime secret from the job log before it can reach one. */
function mask(value, environment) {
  if (environment.GITHUB_ACTIONS === 'true') {
    process.stdout.write(`::add-mask::${value}\n`);
  }
}

function requestHeaders(token, accept) {
  return {
    accept,
    authorization: `Bearer ${token}`,
    'x-github-api-version': API_VERSION,
    'user-agent': USER_AGENT,
  };
}

async function requestJson(url, headers, subject) {
  let response;
  try {
    response = await fetch(url, { headers });
  } catch (error) {
    refuse(EXIT_RETRIEVAL_FAILED, `the ${subject} repository is unreachable: ${error.message}`);
  }
  return response;
}

/**
 * Exchange the application assertion for an installation token. The token is a
 * runtime secret: it is masked, never written down, and never returned to a
 * caller that outlives the fetch.
 */
export async function installationToken(baseUrl, credentials, environment, subject) {
  const assertion = applicationAssertion(credentials);
  let response;
  try {
    response = await fetch(`${baseUrl}/app/installations/${credentials.installationId}/access_tokens`, {
      method: 'POST',
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${assertion}`,
        'x-github-api-version': API_VERSION,
        'user-agent': USER_AGENT,
      },
    });
  } catch (error) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the ${subject} retrieval credential could not be exchanged: ${error.message}`,
    );
  }
  if (response.status !== 201) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the ${subject} retrieval credential was rejected (HTTP ${response.status})`,
    );
  }
  const body = await response.json().catch(() => ({}));
  const token = typeof body.token === 'string' ? body.token : '';
  if (token === '') {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the ${subject} retrieval credential exchange returned no token`,
    );
  }
  mask(token, environment);
  return token;
}

/** Encode a value into URL path segments, leaving its separators as separators. */
function encodePathSegments(value) {
  return value.split('/').map(encodeURIComponent).join('/');
}

/**
 * Fetch the document bytes at the declared fetch revision. The revision is a
 * commit, so the response cannot follow a branch that has moved since the
 * coordinate was declared.
 */
export async function fetchDocument({
  baseUrl, credentials, token, documentPath, fetchRevision, subject, document,
}) {
  const url = `${baseUrl}/repos/${credentials.repository}/contents/`
    + `${encodePathSegments(documentPath)}`
    + `?ref=${fetchRevision}`;
  const response = await requestJson(
    url,
    requestHeaders(token, 'application/vnd.github.raw'),
    subject,
  );
  if (!response.ok) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the ${document} was not retrievable at the declared path and fetch `
      + `revision (HTTP ${response.status})`,
    );
  }
  return Buffer.from(await response.arrayBuffer());
}

/**
 * List the identifiers of the assets published by the release under an exact
 * tag.
 *
 * Only identifiers are returned. The caller owns the refusal for an identifier
 * it cannot find, because which coordinate is missing is the caller's to say,
 * and an asset's name is not something a refusal needs in order to say it.
 */
export async function fetchReleaseAssets({
  baseUrl, credentials, token, releaseTag, subject, document,
}) {
  const url = `${baseUrl}/repos/${credentials.repository}/releases/tags/`
    + `${encodePathSegments(releaseTag)}`;
  const response = await requestJson(
    url,
    requestHeaders(token, 'application/vnd.github+json'),
    subject,
  );
  if (!response.ok) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `no release holding the ${document} is published under the declared tag `
      + `(HTTP ${response.status})`,
    );
  }
  const body = await response.json().catch(() => ({}));
  const assets = Array.isArray(body.assets) ? body.assets : [];
  return Object.freeze(assets.map((asset) => String(asset?.id ?? '')));
}

/**
 * Fetch one release asset by its immutable identifier.
 *
 * The API answers with a redirect to storage whose URL carries its own
 * credential, and the redirect is followed. Two things make that safe. Fetch
 * removes the authorization header on a cross-origin redirect, so the
 * installation token does not reach the storage host. And the caller verifies
 * the returned bytes against a declared digest, so a redirect that answered
 * with anything else is refused rather than packaged.
 */
export async function fetchAssetBytes({
  baseUrl, credentials, token, assetId, subject, document,
}) {
  const url = `${baseUrl}/repos/${credentials.repository}/releases/assets/${assetId}`;
  const response = await requestJson(
    url,
    requestHeaders(token, 'application/octet-stream'),
    subject,
  );
  if (!response.ok) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the ${document} was not retrievable as the declared release asset `
      + `(HTTP ${response.status})`,
    );
  }
  return Buffer.from(await response.arrayBuffer());
}

/**
 * Report whether a revision names a commit in the repository. The caller owns
 * the refusal, because which coordinate is missing is the caller's to say.
 */
export async function commitExists({ baseUrl, credentials, token, revision, subject }) {
  const url = `${baseUrl}/repos/${credentials.repository}/commits/${revision}`;
  const response = await requestJson(
    url,
    requestHeaders(token, 'application/vnd.github+json'),
    subject,
  );
  return Object.freeze({ ok: response.ok, status: response.status });
}

export function contains(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

/**
 * Resolve where a verified document is staged. It must sit under the runner's
 * own temporary directory: not in the workspace, not in an artifact, and not in
 * a cache.
 */
export function resolveOutputPath(environment, declared) {
  if (declared === '') {
    refuse(EXIT_COORDINATE_REJECTED, '--output=<path> is required');
  }
  const runnerTemp = environmentValue(environment, 'RUNNER_TEMP');
  if (runnerTemp === '') {
    refuse(EXIT_COORDINATE_REJECTED, 'RUNNER_TEMP is required; the document is staged there alone');
  }
  const workspace = environmentValue(environment, 'GITHUB_WORKSPACE');
  const temporaryRoot = path.resolve(runnerTemp);
  const output = path.resolve(declared);
  if (workspace !== '' && contains(path.resolve(workspace), temporaryRoot)) {
    refuse(EXIT_COORDINATE_REJECTED, 'RUNNER_TEMP must not resolve inside the workspace');
  }
  if (!contains(temporaryRoot, output)) {
    refuse(EXIT_COORDINATE_REJECTED, '--output must resolve inside RUNNER_TEMP');
  }
  return output;
}
