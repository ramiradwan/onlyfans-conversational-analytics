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
 * Retrieval itself is tools/release-retrieval/github-document.mjs, the one
 * authenticated path a release uses. Every coordinate and credential arrives in
 * the environment rather than on the command line. The document is written only
 * after all four coordinates agree, so a refusal leaves the packaging step no
 * input to read.
 *
 * The verified document also carries the privacy policy instrument the packaged
 * Agent configures, so the release privacy policy URL is derived here and
 * emitted for the packaging step rather than declared a second time.
 */

import { createHash } from 'node:crypto';
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { canonicalLegalBindingsJson } from './canonical-json.mjs';
import {
  COMMIT_REVISION,
  EXIT_COORDINATE_REJECTED,
  EXIT_CREDENTIAL_ABSENT,
  EXIT_DIGEST_MISMATCH,
  EXIT_NOT_CANONICAL,
  EXIT_RETRIEVAL_FAILED,
  EXIT_SOURCE_REVISION_ABSENT,
  EXIT_SOURCE_REVISION_MISMATCH,
  GateRefusal,
  argumentValue,
  commitExists,
  contains,
  environmentValue,
  fetchDocument,
  installationToken,
  refuse,
  resolveApiBaseUrl,
  resolveCredentials as resolveRetrievalCredentials,
  resolveOutputPath,
  validateDocumentPath,
  validateExpectedDigest,
  validateRevision,
} from '../release-retrieval/github-document.mjs';

export {
  EXIT_COORDINATE_REJECTED,
  EXIT_CREDENTIAL_ABSENT,
  EXIT_DIGEST_MISMATCH,
  EXIT_NOT_CANONICAL,
  EXIT_RETRIEVAL_FAILED,
  EXIT_SOURCE_REVISION_ABSENT,
  EXIT_SOURCE_REVISION_MISMATCH,
  GateRefusal,
  resolveOutputPath,
};

/** A verified document that cannot yield the release privacy policy URL. */
export const EXIT_INSTRUMENT_REJECTED = 9;

const SUBJECT = 'Legal';
const DOCUMENT = 'Legal release bindings document';
const PRIVACY_POLICY_INSTRUMENT = 'privacy_policy';

/** The name the derived URL is emitted under for the packaging step to read. */
export const PRIVACY_POLICY_URL_VARIABLE = 'RELEASE_PRIVACY_POLICY_URL';

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

/** Read the four release coordinates. None of them carries a default. */
export function resolveCoordinates(environment) {
  const [sourceRevision, fetchRevision, documentPath, expectedDigest] = COORDINATE_VARIABLES.map(
    (name) => environmentValue(environment, name),
  );
  return Object.freeze({
    sourceRevision: validateRevision('LEGAL_REPOSITORY_REVISION', sourceRevision),
    fetchRevision: validateRevision('LEGAL_BINDINGS_REPOSITORY_REVISION', fetchRevision),
    documentPath: validateDocumentPath('LEGAL_BINDINGS_PATH', documentPath),
    expectedDigest: validateExpectedDigest('LEGAL_BINDINGS_DIGEST', expectedDigest),
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
  return resolveRetrievalCredentials(environment, {
    variables: CREDENTIAL_VARIABLES,
    subject: SUBJECT,
    object: 'the release bindings',
  });
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
    refuse(EXIT_NOT_CANONICAL, `the ${DOCUMENT} is not JSON: ${error.message}`);
  }
  let canonical;
  try {
    canonical = Buffer.from(canonicalLegalBindingsJson(document), 'utf8');
  } catch (error) {
    refuse(EXIT_NOT_CANONICAL, `the ${DOCUMENT} is not canonical JSON: ${error.message}`);
  }
  if (Buffer.compare(fetched, canonical) !== 0) {
    refuse(
      EXIT_NOT_CANONICAL,
      `the ${DOCUMENT} is not stored in its canonical form, `
      + 'so its digest is not the identity the contract defines',
    );
  }
  const digest = createHash('sha256').update(canonical).digest('hex');
  if (digest !== coordinates.expectedDigest) {
    refuse(EXIT_DIGEST_MISMATCH, `the ${DOCUMENT} does not match the declared digest`);
  }
  const embedded = document.legal_repository_revision;
  if (typeof embedded !== 'string' || !COMMIT_REVISION.test(embedded)) {
    refuse(EXIT_SOURCE_REVISION_MISMATCH, `the ${DOCUMENT} carries no source revision`);
  }
  if (embedded !== coordinates.sourceRevision) {
    refuse(
      EXIT_SOURCE_REVISION_MISMATCH,
      `the ${DOCUMENT} was approved at a source revision other than the declared one`,
    );
  }
  return Object.freeze({ digest, bytes: canonical, document });
}

/**
 * Derive the privacy policy URL the packaged Agent configures, by joining the
 * verified document's public origin with the privacy policy instrument's public
 * route.
 *
 * The join is the one the release attestation recomputes from the same document
 * and compares against the packaged configuration, so it is written the same
 * way: trailing separators trimmed from the origin, the absolute route
 * appended, and nothing else. The checks are the conditions under which the
 * packaged URL and the attested URL are the same string, plus the two the
 * extension build imposes on anything it packages. No origin, route or locale
 * is written down here; a document that declares none yields no URL and the
 * release stops.
 */
export function deriveReleasePrivacyPolicyUrl(document) {
  const origin = document?.public_origin;
  const route = document?.instruments?.[PRIVACY_POLICY_INSTRUMENT]?.public_url;
  if (typeof origin !== 'string' || typeof route !== 'string' || route === '') {
    refuse(
      EXIT_INSTRUMENT_REJECTED,
      `the ${DOCUMENT} carries no privacy policy instrument`,
    );
  }
  if (!route.startsWith('/')) {
    refuse(
      EXIT_INSTRUMENT_REJECTED,
      'the privacy policy instrument does not carry an absolute public route',
    );
  }
  const url = `${origin.replace(/\/+$/, '')}${route}`;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    refuse(EXIT_INSTRUMENT_REJECTED, `the ${DOCUMENT} public origin is not a URL`);
  }
  if (parsed.protocol !== 'https:') {
    refuse(
      EXIT_INSTRUMENT_REJECTED,
      'the privacy policy instrument does not resolve over HTTPS',
    );
  }
  if (parsed.hostname.endsWith('.invalid')) {
    refuse(
      EXIT_INSTRUMENT_REJECTED,
      'the privacy policy instrument does not resolve to a production-capable hostname',
    );
  }
  if (parsed.href !== url) {
    refuse(
      EXIT_INSTRUMENT_REJECTED,
      'the privacy policy route does not join the public origin into its own '
      + 'parsed form, so the packaged URL and the attested URL would differ',
    );
  }
  return url;
}

/**
 * Resolve where the derived release values are emitted. The path is optional
 * and names the job's environment file, which is never in the workspace.
 */
export function resolveEnvironmentFilePath(environment, declared) {
  if (declared === '') return '';
  const resolved = path.resolve(declared);
  const workspace = environmentValue(environment, 'GITHUB_WORKSPACE');
  if (workspace !== '' && contains(path.resolve(workspace), resolved)) {
    refuse(EXIT_COORDINATE_REJECTED, '--environment-file must not resolve inside the workspace');
  }
  return resolved;
}

export async function run(argv, environment) {
  const output = resolveOutputPath(environment, argumentValue(argv, '--output'));
  const environmentFile = resolveEnvironmentFilePath(
    environment,
    argumentValue(argv, '--environment-file'),
  );
  assertProductRevision(environment);
  const coordinates = resolveCoordinates(environment);
  const credentials = resolveCredentials(environment);
  const baseUrl = resolveApiBaseUrl(environment, 'LEGAL_BINDINGS_API_BASE_URL');

  const token = await installationToken(baseUrl, credentials, environment, SUBJECT);
  const fetched = await fetchDocument({
    baseUrl,
    credentials,
    token,
    documentPath: coordinates.documentPath,
    fetchRevision: coordinates.fetchRevision,
    subject: SUBJECT,
    document: DOCUMENT,
  });
  const verified = verifyDocumentBytes(fetched, coordinates);
  const privacyPolicyUrl = deriveReleasePrivacyPolicyUrl(verified.document);
  // The declared source revision is read on its own; it is never compared with
  // the fetch revision, which the Legal contract keeps independent.
  const source = await commitExists({
    baseUrl,
    credentials,
    token,
    revision: coordinates.sourceRevision,
    subject: SUBJECT,
  });
  if (!source.ok) {
    refuse(
      EXIT_SOURCE_REVISION_ABSENT,
      'the declared source revision does not name a commit in the Legal '
      + `repository (HTTP ${source.status})`,
    );
  }

  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, verified.bytes);
  const staged = await readFile(output);
  if (Buffer.compare(staged, verified.bytes) !== 0) {
    refuse(EXIT_NOT_CANONICAL, `the staged ${DOCUMENT} changed as it was written`);
  }
  if (environmentFile !== '') {
    await appendFile(
      environmentFile,
      `${PRIVACY_POLICY_URL_VARIABLE}=${privacyPolicyUrl}\n`,
      'utf8',
    );
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
