import assert from 'node:assert/strict';
import { readFile, mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import test from 'node:test';
import { SIGNER_RELEASE, auditSignerArchive, signerReleaseFiles, auditInstalledSigner, auditSignerMetadata } from '../qualification/signer-release.mjs';

test('vendored signer matches the published archive and rejects changed bytes', async () => {
  const bytes = await readFile(new URL(`../vendor/${SIGNER_RELEASE.archive}`, import.meta.url));
  auditSignerArchive(bytes);
  bytes[bytes.length - 1] ^= 1;
  assert.throws(() => auditSignerArchive(bytes), /reviewed release/);
});

test('installed source and export cannot differ from the archive claimed by artifact metadata', async () => {
  const files = signerReleaseFiles(await readFile(new URL(`../vendor/${SIGNER_RELEASE.archive}`, import.meta.url)));
  const root = await mkdtemp(path.join(os.tmpdir(), 'ofca-signer-install-'));
  try {
    for (const [name, bytes] of files) {
      const filename = path.join(root, name);
      await mkdir(path.dirname(filename), { recursive: true });
      await writeFile(filename, bytes);
    }
    const entry = path.join(root, 'src/signing/index.js');
    await auditInstalledSigner({ files, root, entry });
    await assert.rejects(auditInstalledSigner({ files, root, entry: path.join(root, 'src/index.js') }), /reviewed entry/);
    await writeFile(path.join(root, 'src/signing/provider.js'), '// substituted implementation\n');
    await assert.rejects(auditInstalledSigner({ files, root, entry }), /installed signer differs/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('artifact identity binds archive digest and numeric release coordinates', () => {
  const metadata = {
    signer: `${SIGNER_RELEASE.package}@${SIGNER_RELEASE.version}`,
    signer_tarball: `sha256:${SIGNER_RELEASE.sha256}`,
    signer_release: { ...SIGNER_RELEASE },
  };
  auditSignerMetadata(metadata);
  assert.throws(() => auditSignerMetadata({ ...metadata, signer_tarball: `sha256:${'0'.repeat(64)}` }));
  assert.throws(() => auditSignerMetadata({ ...metadata, signer_release: { ...SIGNER_RELEASE, asset_id: 1 } }));
});
