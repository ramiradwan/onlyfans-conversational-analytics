// QA producer lives in the orchestrator, never in the frozen consumer.
// It invokes the immutable rc4 maintainer's actual producer with public test
// scalars, reproducing the same keyset/catalog/object path as production.
import { createPrivateKey } from 'node:crypto';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import vectors from '../node_modules/local-authenticated-read-connector/publication/vectors.json' with { type: 'json' };

export const publicationTrust = vectors.trust;
export const fixtureRule = revision => ({ schema: 'local-packaged-signing-rule/v1', source_revision: revision,
  static_param: `qualification-parameter-${revision}`, format: { prefix: '12345', suffix: 'cafebabe',
    checksum_indexes: [0, 2, 2, 10, 39], checksum_constant: -17 } });

export async function realProducer(signerRoot) {
  const git = async (...args) => (await promisify(execFile)('git', args, { cwd: signerRoot, windowsHide: true })).stdout.trim();
  const commit = await git('rev-parse', 'HEAD'), tree = await git('rev-parse', 'HEAD^{tree}');
  if (commit !== '5c635031fdce2f723470d738b8cfb768e0ed9dbe' || tree !== '90ba4c855bc84e76dd0fbd1b0a0e0e739b70ea63'
    || await git('status', '--porcelain', '--untracked-files=no', '--', 'tools/lib', 'src/signing')) {
    throw new Error('Immutable rc4 producer source required');
  }
  const producerBytes = await readFile(path.join(signerRoot, 'tools/lib/parameter-publication-producer.js'));
  const receipt = { commit, tree, producer_sha256: createHash('sha256').update(producerBytes).digest('hex') };
  const { preparePublisherKeyset, prepareParameterCatalog, parameterObject } = await import(pathToFileURL(
    path.join(signerRoot, 'tools/lib/parameter-publication-producer.js')).href);
  const { packagedRulePublicationJsonText } = await import(pathToFileURL(
    path.join(signerRoot, 'tools/lib/packaged-rule-document.js')).href);
  const seed = JSON.parse(Buffer.from(vectors.fixtures[0].keyset.split('.')[1], 'base64url'));
  const privatePem = (jwk, scalar) => createPrivateKey({ key: { ...jwk,
    d: Buffer.from(Uint8Array.from({ length: 32 }, (_, i) => i === 31 ? scalar : 0)).toString('base64url') }, format: 'jwk' })
    .export({ format: 'pem', type: 'pkcs8' });
  const rootKey = privatePem(publicationTrust.roots[0].jwk, 1), publisherKey = privatePem(seed.keys[0].jwk, 2);
  const now = Math.floor(Date.now() / 1000), files = new Map();
  const keys = await preparePublisherKeyset({ payload: { ...seed, issued_at: now - 60, expires_at: now + 30 * 86400,
    keys: seed.keys.map(key => ({ ...key, not_before: now - 60, not_after: now + 30 * 86400 })) },
    trust: publicationTrust, rootKey, rootKid: 'synthetic-root', now });
  files.set('publisher-keyset.jws', Buffer.from(keys.jws));
  let catalog = null;
  return Object.freeze({ files, receipt,
    async publish(revision, { withdraw = [], empty = false } = {}) {
      const object = empty ? null : await parameterObject(Buffer.from(packagedRulePublicationJsonText(fixtureRule(revision))));
      const next = await prepareParameterCatalog({ current: catalog?.jws ?? null, keyset: keys.jws,
        trust: publicationTrust, publisherKey, publisherKid: 'synthetic-publisher', object, withdraw,
        replaceRules: empty, now: Math.floor(Date.now() / 1000) });
      if (object) files.set(object.path, Buffer.from(object.bytes));
      files.set('catalog.jws', Buffer.from(next.jws));
      catalog = next;
      return { sequence: next.verified.payload.sequence,
        revisions: next.verified.payload.rules.map(entry => entry.source_revision) };
    },
  });
}
