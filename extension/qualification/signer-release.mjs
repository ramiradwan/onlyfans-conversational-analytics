import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { gunzipSync } from 'fflate';

// Reviewed GitHub release coordinates. Updating a lockfile alone must not authorize
// different archive bytes, even when the package keeps the same version string.
export const SIGNER_RELEASE = Object.freeze({
  package: 'local-authenticated-read-connector',
  version: '0.5.0-rc.1',
  repository: 'ramiradwan/local-of-signer',
  tag: 'v0.5.0-rc.1',
  release_id: 402765184,
  asset_id: 608867698,
  archive: 'local-authenticated-read-connector-0.5.0-rc.1.tgz',
  sha256: '9a4a1143aae37b47b99ed3ac81e4a949d32e17d7785686d0136de9cc859e46a9',
  source_revision: 'd8cc1812b7282097c689ceceed766792c9c1e629',
  source_tree: '37963ea0e3afa03f4613fd9150c64efb5ba920b4',
});

export function auditSignerArchive(bytes) {
  assert.equal(createHash('sha256').update(bytes).digest('hex'), SIGNER_RELEASE.sha256,
    'signer archive differs from the reviewed release');
}

// This parser accepts only the already digest-verified release, whose npm tar
// contains regular files. It is not a general archive extraction facility.
export function signerReleaseFiles(archiveBytes) {
  auditSignerArchive(archiveBytes);
  const tar = gunzipSync(archiveBytes);
  const files = new Map();
  const field = (offset, length) => Buffer.from(tar.subarray(offset, offset + length))
    .toString('utf8').split('\0')[0];
  for (let offset = 0; offset + 512 <= tar.length;) {
    if (tar.subarray(offset, offset + 512).every((byte) => byte === 0)) break;
    const prefix = field(offset + 345, 155);
    const name = `${prefix ? `${prefix}/` : ''}${field(offset, 100)}`;
    assert.match(name, /^package\//);
    const relative = name.slice('package/'.length);
    assert.ok(relative && !relative.includes('\\') && !relative.split('/').some((part) => !part || part === '..' || part === '.'));
    assert.equal(field(offset + 156, 1), '0', 'reviewed signer archive contains a non-regular entry');
    const sizeField = field(offset + 124, 12).trim();
    assert.match(sizeField, /^[0-7]+$/);
    const size = Number.parseInt(sizeField, 8);
    assert.ok(Number.isSafeInteger(size) && offset + 512 + size <= tar.length);
    assert.equal(files.has(relative), false);
    files.set(relative, Buffer.from(tar.subarray(offset + 512, offset + 512 + size)));
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  assert.ok(files.has('package.json') && files.has('src/signing/index.js') && files.has('LICENSE'));
  return files;
}

export async function auditInstalledSigner({ files, root, entry }) {
  assert.equal(await realpath(entry), await realpath(path.join(root, 'src/signing/index.js')),
    'public browser-signing export differs from the reviewed entry');
  for (const [name, expected] of files) {
    const actual = await readFile(path.join(root, name));
    assert.equal(Buffer.compare(actual, expected), 0, `installed signer differs from the reviewed archive: ${name}`);
  }
}

export function auditSignerMetadata(metadata) {
  assert.equal(metadata.signer, `${SIGNER_RELEASE.package}@${SIGNER_RELEASE.version}`);
  assert.equal(metadata.signer_tarball, `sha256:${SIGNER_RELEASE.sha256}`);
  assert.deepEqual(metadata.signer_release, SIGNER_RELEASE);
}
