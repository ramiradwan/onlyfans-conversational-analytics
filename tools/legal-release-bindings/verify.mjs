#!/usr/bin/env node
/**
 * Retrieve and verify the Legal release bindings a Store candidate is built
 * against, and stage the verified document on ephemeral runner storage.
 *
 * Provenance is four coordinates, all declared by the operator and none of them
 * inferred from repository state:
 *
 *   A  LEGAL_REPOSITORY_REVISION             the embedded source/approval revision
 *   B  LEGAL_BINDINGS_REPOSITORY_REVISION    the revision the artifact is fetched from
 *      LEGAL_BINDINGS_PATH                   the exact path in the Legal repository
 *      LEGAL_BINDINGS_DIGEST                 the expected canonical SHA-256
 *
 * A and B are distinct coordinates. The document is fetched at B and its
 * embedded A is validated separately; nothing here requires them to be equal.
 *
 * Every coordinate and credential arrives in the environment rather than on the
 * command line. The document is written only after all four coordinates agree,
 * so a refusal leaves the packaging step no input to read.
 */

import { createHash, createSign } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { canonicalLegalBindingsJson } from './canonical-json.mjs';

export const EXIT_COORDINATE_REJECTED = 2;
export const EXIT_CREDENTIAL_ABSENT = 3;
export const EXIT_RETRIEVAL_FAILED = 4;
export const EXIT_NOT_CANONICAL = 5;
export const EXIT_DIGEST_MISMATCH = 6;
export const EXIT_SOURCE_REVISION_MISMATCH = 7;
export const EXIT_SOURCE_REVISION_ABSENT = 8;

const COMMIT_REVISION = /^[0-9a-f]{40}$/;
const CANONICAL_DIGEST = /^[0-9a-f]{64}$/;
const REPOSITORY = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;
const NUMERIC_IDENTIFIER = /^[0-9]+$/;
const PUBLIC_API_BASE_URL = 'https://api.github.com';
const API_VERSION = '2022-11-28';
const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', 'localhost']);

// The coordinates and credentials this gate reads, named so a failure says
// which one to supply without printing any value.
const COORDINATE_VARIABLES = Object.freeze([
  'LEGAL_REPOSITORY_REVISION',
  'LEGAL_BINDINGS_REPOSITORY_REVISION',
  'LEGAL_BINDINGS_PATH',
  'LEGAL_BINDINGS_DIGEST',
]);
const CREDENTIAL_VARIABLES = Object.freeze([
  'LEGAL_BINDINGS_REPOSITORY',
  'LEGAL_BINDINGS_APP_ID',
  'LEGAL_BINDINGS_APP_PRIVATE_KEY_B64',
  'LEGAL_BINDINGS_INSTALLATION_ID',
]);

/** A refusal carrying the exit code that names which check refused. */
export class GateRefusal extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'GateRefusal';
    this.code = code;
  }
}

function refuse(code, message) {
  throw new GateRefusal(code, message);
}

function environmentValue(environment, name) {
  const value = environment[name];
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * Resolve the API origin. The public origin is the only non-loopback value the
 * gate accepts, so the override is a test seam and cannot redirect a release to
 * a third party.
 */
function resolveApiBaseUrl(environment) {
  const declared = environmentValue(environment, 'LEGAL_BINDINGS_API_BASE_URL');
  if (declared === '') return PUBLIC_API_BASE_URL;
  let parsed;
  try {
    parsed = new URL(declared);
  } catch {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_API_BASE_URL is not a URL');
  }
  if (!LOOPBACK_HOSTS.has(parsed.hostname.replace(/^\[|\]$/g, ''))) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      'LEGAL_BINDINGS_API_BASE_URL may only name a loopback address',
    );
  }
  return parsed.origin;
}

/** A repository path: relative, JSON, and free of traversal or separators. */
function validateDocumentPath(value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_PATH is required and has no default');
  }
  if (value.startsWith('/') || value.includes('\\') || value.includes('//')) {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_PATH must be a relative repository path');
  }
  const segments = value.split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_PATH must not contain an empty or relative segment');
  }
  if (!value.endsWith('.json')) {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_PATH must name a JSON document');
  }
  return value;
}

function validateRevision(name, value) {
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

function validateExpectedDigest(value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_DIGEST is required and has no default');
  }
  if (value.startsWith('sha256:')) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      'LEGAL_BINDINGS_DIGEST is bare lowercase 64-hex, not the sha256:-prefixed form',
    );
  }
  if (!CANONICAL_DIGEST.test(value)) {
    refuse(EXIT_COORDINATE_REJECTED, 'LEGAL_BINDINGS_DIGEST must be bare lowercase 64-hex');
  }
  return value;
}

/** Read the four release coordinates. None of them carries a default. */
export function resolveCoordinates(environment) {
  const [sourceRevision, fetchRevision, documentPath, expectedDigest] = COORDINATE_VARIABLES.map(
    (name) => environmentValue(environment, name),
  );
  return Object.freeze({
    sourceRevision: validateRevision('LEGAL_REPOSITORY_REVISION', sourceRevision),
    fetchRevision: validateRevision('LEGAL_BINDINGS_REPOSITORY_REVISION', fetchRevision),
    documentPath: validateDocumentPath(documentPath),
    expectedDigest: validateExpectedDigest(expectedDigest),
  });
}

/**
 * Bind the tree being packaged to the declared Product revision.
 *
 * This is not the source/fetch revision pair, which the Legal contract keeps
 * independent and which nothing here compares. Both values read below name the
 * same thing twice: the revision the run was dispatched at is the revision
 * whose workflow definition executes, and the declared Product revision is the
 * revision checked out and packaged. Requiring those to agree keeps the gate
 * and the source it gates on one commit.
 */
export function assertProductRevision(environment) {
  const declared = validateRevision(
    'PRODUCT_REVISION',
    environmentValue(environment, 'PRODUCT_REVISION'),
  );
  const dispatched = environmentValue(environment, 'GITHUB_SHA');
  if (dispatched === '') {
    refuse(EXIT_COORDINATE_REJECTED, 'GITHUB_SHA is required; the release runs from a revision');
  }
  if (declared !== dispatched) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      'the declared Product revision is not the revision this run was dispatched at, '
      + 'so the workflow definition and the packaged tree are different commits',
    );
  }
  return declared;
}

/**
 * Read the retrieval credentials. They are absent until the Legal retrieval
 * application is installed, and absence stops the release here rather than
 * degrading to an unauthenticated or a skipped fetch.
 */
export function resolveCredentials(environment) {
  const absent = CREDENTIAL_VARIABLES.filter((name) => environmentValue(environment, name) === '');
  if (absent.length > 0) {
    refuse(
      EXIT_CREDENTIAL_ABSENT,
      'the Legal retrieval credential is not configured, so the release bindings '
      + `cannot be verified: ${absent.join(', ')}`,
    );
  }
  const repository = environmentValue(environment, 'LEGAL_BINDINGS_REPOSITORY');
  const applicationId = environmentValue(environment, 'LEGAL_BINDINGS_APP_ID');
  const installationId = environmentValue(environment, 'LEGAL_BINDINGS_INSTALLATION_ID');
  if (!REPOSITORY.test(repository)) {
    refuse(EXIT_CREDENTIAL_ABSENT, 'LEGAL_BINDINGS_REPOSITORY must be owner/name');
  }
  for (const [name, value] of [
    ['LEGAL_BINDINGS_APP_ID', applicationId],
    ['LEGAL_BINDINGS_INSTALLATION_ID', installationId],
  ]) {
    if (!NUMERIC_IDENTIFIER.test(value)) {
      refuse(EXIT_CREDENTIAL_ABSENT, `${name} must be a numeric identifier`);
    }
  }
  let privateKey;
  try {
    privateKey = Buffer.from(
      environmentValue(environment, 'LEGAL_BINDINGS_APP_PRIVATE_KEY_B64'),
      'base64',
    ).toString('utf8');
  } catch {
    privateKey = '';
  }
  if (!privateKey.includes('PRIVATE KEY')) {
    refuse(
      EXIT_CREDENTIAL_ABSENT,
      'LEGAL_BINDINGS_APP_PRIVATE_KEY_B64 must be a base64-encoded PEM private key',
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

async function requestJson(url, headers) {
  let response;
  try {
    response = await fetch(url, { headers });
  } catch (error) {
    refuse(EXIT_RETRIEVAL_FAILED, `the Legal repository is unreachable: ${error.message}`);
  }
  return response;
}

/**
 * Exchange the application assertion for an installation token. The token is a
 * runtime secret: it is masked, never written down, and never returned to a
 * caller that outlives the fetch.
 */
async function installationToken(baseUrl, credentials, environment) {
  const assertion = applicationAssertion(credentials);
  let response;
  try {
    response = await fetch(`${baseUrl}/app/installations/${credentials.installationId}/access_tokens`, {
      method: 'POST',
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${assertion}`,
        'x-github-api-version': API_VERSION,
        'user-agent': 'release-bindings-gate',
      },
    });
  } catch (error) {
    refuse(EXIT_RETRIEVAL_FAILED, `the Legal retrieval credential could not be exchanged: ${error.message}`);
  }
  if (response.status !== 201) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      `the Legal retrieval credential was rejected (HTTP ${response.status})`,
    );
  }
  const body = await response.json().catch(() => ({}));
  const token = typeof body.token === 'string' ? body.token : '';
  if (token === '') {
    refuse(EXIT_RETRIEVAL_FAILED, 'the Legal retrieval credential exchange returned no token');
  }
  mask(token, environment);
  return token;
}

function contentHeaders(token, accept) {
  return {
    accept,
    authorization: `Bearer ${token}`,
    'x-github-api-version': API_VERSION,
    'user-agent': 'release-bindings-gate',
  };
}

function encodeRepositoryPath(documentPath) {
  return documentPath.split('/').map(encodeURIComponent).join('/');
}

/**
 * Fetch the document bytes at the declared fetch revision. The revision is a
 * commit, so the response cannot follow a branch that has moved since the
 * coordinate was declared.
 */
async function fetchBindingsDocument(baseUrl, credentials, coordinates, token) {
  const url = `${baseUrl}/repos/${credentials.repository}/contents/`
    + `${encodeRepositoryPath(coordinates.documentPath)}`
    + `?ref=${coordinates.fetchRevision}`;
  const response = await requestJson(url, contentHeaders(token, 'application/vnd.github.raw'));
  if (!response.ok) {
    refuse(
      EXIT_RETRIEVAL_FAILED,
      'the Legal release bindings document was not retrievable at the declared '
      + `path and fetch revision (HTTP ${response.status})`,
    );
  }
  return Buffer.from(await response.arrayBuffer());
}

/**
 * Confirm the declared source revision names a commit in the Legal repository.
 * This reads the source coordinate on its own; it never compares it with the
 * fetch revision, which the Legal contract keeps independent.
 */
async function assertSourceRevisionExists(baseUrl, credentials, coordinates, token) {
  const url = `${baseUrl}/repos/${credentials.repository}/commits/${coordinates.sourceRevision}`;
  const response = await requestJson(url, contentHeaders(token, 'application/vnd.github+json'));
  if (!response.ok) {
    refuse(
      EXIT_SOURCE_REVISION_ABSENT,
      'the declared source revision does not name a commit in the Legal '
      + `repository (HTTP ${response.status})`,
    );
  }
}

/**
 * Verify fetched bytes against the declared coordinates.
 *
 * The stored bytes must already be the contract's canonical form, so a
 * semantically equivalent but differently serialized document is a distinct
 * refusal rather than a different digest. A duplicate member name collapses in
 * the parse and is caught by the same byte comparison.
 */
export function verifyDocumentBytes(fetched, coordinates) {
  let document;
  try {
    document = JSON.parse(fetched.toString('utf8'));
  } catch (error) {
    refuse(EXIT_NOT_CANONICAL, `the Legal release bindings document is not JSON: ${error.message}`);
  }
  let canonical;
  try {
    canonical = Buffer.from(canonicalLegalBindingsJson(document), 'utf8');
  } catch (error) {
    refuse(EXIT_NOT_CANONICAL, `the Legal release bindings document is not canonical JSON: ${error.message}`);
  }
  if (Buffer.compare(fetched, canonical) !== 0) {
    refuse(
      EXIT_NOT_CANONICAL,
      'the Legal release bindings document is not stored in its canonical form, '
      + 'so its digest is not the identity the contract defines',
    );
  }
  const digest = createHash('sha256').update(canonical).digest('hex');
  if (digest !== coordinates.expectedDigest) {
    refuse(
      EXIT_DIGEST_MISMATCH,
      'the Legal release bindings document does not match the declared digest',
    );
  }
  const embedded = document.legal_repository_revision;
  if (typeof embedded !== 'string' || !COMMIT_REVISION.test(embedded)) {
    refuse(
      EXIT_SOURCE_REVISION_MISMATCH,
      'the Legal release bindings document carries no source revision',
    );
  }
  if (embedded !== coordinates.sourceRevision) {
    refuse(
      EXIT_SOURCE_REVISION_MISMATCH,
      'the Legal release bindings document was approved at a source revision '
      + 'other than the declared one',
    );
  }
  return Object.freeze({ digest, bytes: canonical });
}

/**
 * Resolve where the verified document is staged. It must sit under the
 * runner's own temporary directory: not in the workspace, not in an artifact,
 * and not in a cache.
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

function contains(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function argumentValue(argv, name) {
  const prefix = `${name}=`;
  const inline = argv.find((argument) => argument.startsWith(prefix));
  if (inline !== undefined) return inline.slice(prefix.length).trim();
  const index = argv.indexOf(name);
  if (index === -1) return '';
  const next = argv[index + 1];
  return typeof next === 'string' ? next.trim() : '';
}

export async function run(argv, environment) {
  const output = resolveOutputPath(environment, argumentValue(argv, '--output'));
  assertProductRevision(environment);
  const coordinates = resolveCoordinates(environment);
  const credentials = resolveCredentials(environment);
  const baseUrl = resolveApiBaseUrl(environment);

  const token = await installationToken(baseUrl, credentials, environment);
  const fetched = await fetchBindingsDocument(baseUrl, credentials, coordinates, token);
  const verified = verifyDocumentBytes(fetched, coordinates);
  await assertSourceRevisionExists(baseUrl, credentials, coordinates, token);

  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, verified.bytes);
  const staged = await readFile(output);
  if (Buffer.compare(staged, verified.bytes) !== 0) {
    refuse(EXIT_NOT_CANONICAL, 'the staged Legal release bindings document changed as it was written');
  }
  return verified.digest;
}

async function main() {
  try {
    const digest = await run(process.argv.slice(2), process.env);
    process.stdout.write(`Legal release bindings verified (${digest}).\n`);
  } catch (error) {
    if (error instanceof GateRefusal) {
      process.stderr.write(`Legal release bindings refused: ${error.message}\n`);
      process.exitCode = error.code;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) await main();
