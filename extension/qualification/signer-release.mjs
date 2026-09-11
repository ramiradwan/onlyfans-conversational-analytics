import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { gunzipSync } from 'fflate';

// Reviewed GitHub release coordinates. Updating a lockfile alone must not authorize
// different archive bytes, even when the package keeps the same version string.
export const SIGNER_RELEASE = Object.freeze({
  package: 'local-authenticated-read-connector',
  version: '0.2.0',
  repository: 'ramiradwan/local-of-signer',
  tag: 'v0.2.0',
  release_id: 386910504,
  asset_id: 556904013,
  archive: 'local-authenticated-read-connector-0.2.0.tgz',
  sha256: 'd32da93c6c1e863b49ca96ed0bc86f2c9c615174b88ccee04310f817dd88a281',
  source_revision: '672ba94db26aa582451c817ec72a04304a2a238c',
  source_tree: '8f42fe8ac4044478a9c62b0de35a47cbbdd43e7b',
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
