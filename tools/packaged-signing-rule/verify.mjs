#!/usr/bin/env node
/**
 * Retrieve and verify the packaged signing rule a Store candidate is built
 * against, and stage the verified document on ephemeral runner storage.
 *
 * The production rule is held in a private repository and is never checked in
 * here. It reaches a release as declared coordinates, retrieved over the one
 * authenticated path and verified before the build reads it.
 *
 *   SIGNING_RULE_RELEASE_TAG           the release publishing the rule
 *   SIGNING_RULE_RELEASE_ASSET_ID      the immutable identifier of that asset
 *   SIGNING_RULE_DIGEST                the expected SHA-256 of the document
 *   SIGNING_RULE_SOURCE_REVISION       the platform revision the rule reproduces
 *
 * Unlike the Legal bindings document, the rule is retrieved as a release asset
 * rather than at a repository path and revision. It is emitted from a private
 * configuration into a working directory the signer repository refuses to
 * commit from, so no revision holds it and there is no path to declare. A
 * release asset is retrievable and digest-pinned without entering a repository
 * history, which is what keeps the sequence of rules a release has shipped out
 * of a permanent record.
 *
 * The tag and the asset identifier are a pair. A tag can be repointed, so the
 * gate refuses unless the release the tag resolves to publishes exactly the
 * declared asset; the identifier is the coordinate that cannot move.
 *
 * The source revision is not a commit. It is the platform revision the rule
 * signs for, which the rule carries as its own ``source_revision`` and which the
 * Agent matches against an observed request, so it is validated against the
 * schema's own bounds rather than as a Git object.
 *
 * The document is written only once every coordinate agrees, so a refusal
 * leaves the packaging step no rule to read.
 */

import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import {
  EXIT_COORDINATE_REJECTED,
  EXIT_DIGEST_MISMATCH,
  EXIT_NOT_CANONICAL,
  EXIT_SOURCE_REVISION_MISMATCH,
  GateRefusal,
  argumentValue,
  environmentValue,
  fetchAssetBytes,
  fetchReleaseAssets,
  installationToken,
  refuse,
  resolveApiBaseUrl,
  resolveCredentials,
  resolveOutputPath,
  validateAssetIdentifier,
  validateExpectedDigest,
  validateReleaseTag,
} from '../release-retrieval/github-document.mjs';

/** A retrieved document that is not a packaged signing rule at all. */
export const EXIT_SCHEMA_REJECTED = 9;

/** A declared asset the release under the declared tag does not publish. */
export const EXIT_ASSET_ABSENT = 10;

const SUBJECT = 'signing rule';
const DOCUMENT = 'packaged signing rule';
const SCHEMA = 'local-packaged-signing-rule/v1';

// The schema's member names, in the order the packaged serialization writes
// them. The extension build repacks the rule as stableJson over exactly these,
// so a document that is already stored that way packages byte for byte and the
// declared digest is the digest of the rule that ships.
const RULE_MEMBERS = Object.freeze(['schema', 'source_revision', 'static_param', 'format']);
const FORMAT_MEMBERS = Object.freeze([
  'prefix',
  'suffix',
  'checksum_indexes',
  'checksum_constant',
]);

// The platform revision bounds the packaged rule schema itself sets.
const SOURCE_REVISION_LIMIT = 128;

const COORDINATE_VARIABLES = Object.freeze([
  'SIGNING_RULE_RELEASE_TAG',
  'SIGNING_RULE_RELEASE_ASSET_ID',
  'SIGNING_RULE_DIGEST',
  'SIGNING_RULE_SOURCE_REVISION',
]);
const CREDENTIAL_VARIABLES = Object.freeze([
  'SIGNING_RULE_REPOSITORY',
  'SIGNING_RULE_APP_ID',
  'SIGNING_RULE_APP_PRIVATE_KEY_B64',
  'SIGNING_RULE_INSTALLATION_ID',
]);

export function validateSourceRevision(name, value) {
  if (value === '') {
    refuse(EXIT_COORDINATE_REJECTED, `${name} is required and has no default`);
  }
  if (value.length > SOURCE_REVISION_LIMIT || /[\r\n]/.test(value)) {
    refuse(
      EXIT_COORDINATE_REJECTED,
      `${name} is not a platform revision the packaged rule schema can carry`,
    );
  }
  return value;
}

/** Read the four release coordinates. None of them carries a default. */
export function resolveCoordinates(environment) {
  const [releaseTag, assetId, expectedDigest, sourceRevision] = COORDINATE_VARIABLES.map(
    (name) => environmentValue(environment, name),
  );
  return Object.freeze({
    releaseTag: validateReleaseTag('SIGNING_RULE_RELEASE_TAG', releaseTag),
    assetId: validateAssetIdentifier('SIGNING_RULE_RELEASE_ASSET_ID', assetId),
    expectedDigest: validateExpectedDigest('SIGNING_RULE_DIGEST', expectedDigest),
    sourceRevision: validateSourceRevision('SIGNING_RULE_SOURCE_REVISION', sourceRevision),
  });
}

function membersInOrder(value, expected) {
  const actual = Object.keys(value);
  return actual.length === expected.length && actual.every((name, index) => name === expected[index]);
}

/**
 * Verify fetched bytes against the declared coordinates.
 *
 * The stored bytes must already be the serialization the extension build packs
 * the rule under, so the declared digest names the bytes that ship rather than
 * a document that will be rewritten on the way into the archive.
 */
export function verifyDocumentBytes(fetched, coordinates) {
  let document;
  try {
    document = JSON.parse(fetched.toString('utf8'));
  } catch (error) {
    refuse(EXIT_NOT_CANONICAL, `the ${DOCUMENT} is not JSON: ${error.message}`);
  }
  if (typeof document !== 'object' || document === null || Array.isArray(document)) {
    refuse(EXIT_SCHEMA_REJECTED, `the ${DOCUMENT} is not an object`);
  }
  if (document.schema !== SCHEMA) {
    refuse(EXIT_SCHEMA_REJECTED, `the ${DOCUMENT} does not carry the ${SCHEMA} schema`);
  }
  const format = document.format;
  if (
    !membersInOrder(document, RULE_MEMBERS)
    || typeof format !== 'object'
    || format === null
    || Array.isArray(format)
    || !membersInOrder(format, FORMAT_MEMBERS)
  ) {
    refuse(
      EXIT_SCHEMA_REJECTED,
      `the ${DOCUMENT} does not carry the schema's members in the order the `
      + 'packaged serialization writes them',
    );
  }
  const packaged = Buffer.from(`${JSON.stringify(document, null, 2)}\n`, 'utf8');
  if (Buffer.compare(fetched, packaged) !== 0) {
    refuse(
      EXIT_NOT_CANONICAL,
      `the ${DOCUMENT} is not stored in the serialization the package uses, `
      + 'so its digest is not the digest of the rule that would ship',
    );
  }
  const digest = createHash('sha256').update(fetched).digest('hex');
  if (digest !== coordinates.expectedDigest) {
    refuse(EXIT_DIGEST_MISMATCH, `the ${DOCUMENT} does not match the declared digest`);
  }
  const embedded = document.source_revision;
  if (typeof embedded !== 'string' || embedded === '') {
    refuse(EXIT_SOURCE_REVISION_MISMATCH, `the ${DOCUMENT} carries no source revision`);
  }
  if (embedded !== coordinates.sourceRevision) {
    refuse(
      EXIT_SOURCE_REVISION_MISMATCH,
      `the ${DOCUMENT} reproduces a source revision other than the declared one`,
    );
  }
  return Object.freeze({ digest, bytes: fetched });
}

export async function run(argv, environment) {
  const output = resolveOutputPath(environment, argumentValue(argv, '--output'));
  const coordinates = resolveCoordinates(environment);
  const credentials = resolveCredentials(environment, {
    variables: CREDENTIAL_VARIABLES,
    subject: SUBJECT,
    object: 'the packaged signing rule',
  });
  const baseUrl = resolveApiBaseUrl(environment, 'SIGNING_RULE_API_BASE_URL');

  const token = await installationToken(baseUrl, credentials, environment, SUBJECT);
  // The tag locates a release; the asset identifier names the document. Reading
  // the release first is what makes the pair a cross-check: an identifier the
  // resolved release does not publish is refused here rather than fetched and
  // left for the digest to catch, so a tag and an asset naming two different
  // releases cannot produce a build.
  const published = await fetchReleaseAssets({
    baseUrl,
    credentials,
    token,
    releaseTag: coordinates.releaseTag,
    subject: SUBJECT,
    document: DOCUMENT,
  });
  if (!published.includes(coordinates.assetId)) {
    refuse(
      EXIT_ASSET_ABSENT,
      `the release published under the declared tag does not carry the declared `
      + 'asset, so the two coordinates name different releases',
    );
  }
  const fetched = await fetchAssetBytes({
    baseUrl,
    credentials,
    token,
    assetId: coordinates.assetId,
    subject: SUBJECT,
    document: DOCUMENT,
  });
  const verified = verifyDocumentBytes(fetched, coordinates);

  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, verified.bytes);
  const staged = await readFile(output);
  if (Buffer.compare(staged, verified.bytes) !== 0) {
    refuse(EXIT_NOT_CANONICAL, `the staged ${DOCUMENT} changed as it was written`);
  }
  return verified.digest;
}

async function main() {
  try {
    const digest = await run(process.argv.slice(2), process.env);
    process.stdout.write(`Packaged signing rule verified (${digest}).\n`);
  } catch (error) {
    if (error instanceof GateRefusal) {
      process.stderr.write(`Packaged signing rule refused: ${error.message}\n`);
      process.exitCode = error.code;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) await main();
