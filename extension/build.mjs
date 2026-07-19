import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import {
  copyFile,
  mkdir,
  readFile,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.join(ROOT, 'dist');
const SIGNER_PACKAGE = 'local-authenticated-read-connector';
const SIGNER_VERSION = '0.2.0-beta.6';
const SIGNER_SPEC =
  'file:vendor/local-authenticated-read-connector-0.2.0-beta.6.tgz';
const SIGNER_TARBALL = path.join(
  ROOT,
  'vendor',
  'local-authenticated-read-connector-0.2.0-beta.6.tgz',
);
const EXPECTED_PERMISSIONS = Object.freeze([
  'alarms',
  'scripting',
  'storage',
  'unlimitedStorage',
  'webRequest',
]);
const EXPECTED_HOST_PERMISSIONS = Object.freeze([
  'https://onlyfans.com/*',
  'http://bridge.localhost/*',
]);
const EXPECTED_EXTERNAL_MATCHES = Object.freeze(['http://bridge.localhost/*']);
const EXPECTED_EXTENSION_CSP = "script-src 'self'; object-src 'self'; connect-src 'self' http://bridge.localhost:17871 ws://bridge.localhost:17871;";
const FORBIDDEN_PERMISSIONS = Object.freeze([
  'cookies',
  'debugger',
  'nativeMessaging',
  'offscreen',
  'tabs',
  'webRequestBlocking',
]);
const SCRIPT_FILES = Object.freeze(['background.js', 'content.js', 'page-hook.js']);
const NOTICE_FILE = 'THIRD_PARTY_NOTICES.txt';
const ICON_FILES = Object.freeze(['icons/icon48.png', 'icons/icon128.png']);
const TEXT_ENCODER = new TextEncoder();

function sha256(value) {
  return `sha256:${createHash('sha256').update(value).digest('hex')}`;
}

function sha512Integrity(value) {
  return `sha512-${createHash('sha512').update(value).digest('base64')}`;
}

function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function outputBytes(result, filename) {
  const suffix = `/${filename}`;
  const output = result.outputFiles.find((candidate) => (
    candidate.path.replaceAll('\\', '/').endsWith(suffix)
  ));
  if (!output) throw new Error(`esbuild did not produce ${filename}`);
  return output.contents;
}

async function compileOnce() {
  const common = {
    bundle: true,
    charset: 'utf8',
    legalComments: 'none',
    logLevel: 'silent',
    metafile: true,
    minify: false,
    platform: 'browser',
    sourcemap: false,
    target: ['chrome116'],
    treeShaking: true,
    write: false,
  };
  const [background, content, pageHook] = await Promise.all([
    build({
      ...common,
      format: 'esm',
      outfile: path.join(DIST, 'background.js'),
      stdin: {
        contents: [
          `export { createChromeBrowserSigningProvider } from '${SIGNER_PACKAGE}/browser-signing';`,
          "export * from './background.js';",
        ].join('\n'),
        loader: 'js',
        resolveDir: ROOT,
        sourcefile: 'background.bundle-entry.mjs',
      },
    }),
    build({
      ...common,
      entryPoints: [path.join(ROOT, 'content.js')],
      format: 'iife',
      outfile: path.join(DIST, 'content.js'),
    }),
    build({
      ...common,
      entryPoints: [path.join(ROOT, 'page-hook.js')],
      format: 'iife',
      outfile: path.join(DIST, 'page-hook.js'),
    }),
  ]);

  const signerInputs = Object.keys(background.metafile.inputs)
    .map((input) => input.replaceAll('\\', '/'))
    .filter((input) => input.includes(`${SIGNER_PACKAGE}/src/signing/`));
  assert.ok(signerInputs.length > 0, 'background bundle did not include the local signer package');

  return new Map([
    ['background.js', outputBytes(background, 'background.js')],
    ['content.js', outputBytes(content, 'content.js')],
    ['page-hook.js', outputBytes(pageHook, 'page-hook.js')],
  ]);
}

function verifyIdenticalBuilds(first, second) {
  assert.deepEqual([...first.keys()], [...second.keys()]);
  for (const [filename, bytes] of first) {
    assert.equal(
      sha256(bytes),
      sha256(second.get(filename)),
      `${filename} was not byte-for-byte deterministic`,
    );
  }
}

async function readJson(filename) {
  return JSON.parse(await readFile(filename, 'utf8'));
}

function auditManifest(manifest) {
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.minimum_chrome_version, '116');
  assert.deepEqual(manifest.permissions, EXPECTED_PERMISSIONS);
  assert.deepEqual(manifest.host_permissions, EXPECTED_HOST_PERMISSIONS);
  assert.deepEqual(manifest.externally_connectable?.matches, EXPECTED_EXTERNAL_MATCHES);
  assert.equal(manifest.background?.service_worker, 'background.js');
  assert.equal(manifest.background?.type, 'module');
  assert.deepEqual(
    manifest.content_scripts?.map((entry) => entry.js),
    [['page-hook.js'], ['content.js']],
  );
  const declared = new Set([
    ...(manifest.permissions ?? []),
    ...(manifest.optional_permissions ?? []),
  ]);
  for (const permission of FORBIDDEN_PERMISSIONS) {
    assert.equal(declared.has(permission), false, `forbidden permission declared: ${permission}`);
  }
  assert.equal(manifest.optional_host_permissions, undefined);
  const policy = manifest.content_security_policy?.extension_pages ?? '';
  assert.equal(policy, EXPECTED_EXTENSION_CSP);
  assert.equal(policy.includes("script-src 'self'"), true);
  assert.equal(policy.includes("object-src 'self'"), true);
  assert.equal(policy.includes("'unsafe-eval'"), false);
  assert.equal(policy.includes('your-'), false);
}

function auditExecutableSource(filename, source) {
  const forbidden = [
    /\beval\s*\(/,
    /\bnew\s+Function\s*\(/,
    /\bimportScripts\s*\(\s*['"`]https?:\/\//i,
    /\bimport\s*\(\s*['"`]https?:\/\//i,
    /\bfrom\s*['"`]https?:\/\//i,
  ];
  for (const expression of forbidden) {
    assert.equal(expression.test(source), false, `${filename} contains forbidden executable code`);
  }
}

async function auditDependencyLock() {
  const packageDocument = await readJson(path.join(ROOT, 'package.json'));
  assert.equal(packageDocument.dependencies?.[SIGNER_PACKAGE], SIGNER_SPEC);
  assert.equal(packageDocument.devDependencies?.esbuild, '0.25.6');

  const lock = await readJson(path.join(ROOT, 'package-lock.json'));
  const root = lock.packages?.[''];
  const signer = lock.packages?.[`node_modules/${SIGNER_PACKAGE}`];
  assert.equal(root?.dependencies?.[SIGNER_PACKAGE], SIGNER_SPEC);
  assert.equal(signer?.version, SIGNER_VERSION);
  assert.equal(signer?.resolved, SIGNER_SPEC);
  assert.equal(signer?.integrity, sha512Integrity(await readFile(SIGNER_TARBALL)));

  const installed = await readJson(path.join(
    ROOT,
    'node_modules',
    SIGNER_PACKAGE,
    'package.json',
  ));
  assert.equal(installed.version, SIGNER_VERSION);
}

async function auditArtifact() {
  await auditDependencyLock();
  const manifest = await readJson(path.join(DIST, 'manifest.json'));
  auditManifest(manifest);

  const metadata = await readJson(path.join(DIST, 'build-meta.json'));
  assert.equal(metadata.schema, 'ofca-extension-build/v1');
  assert.equal(metadata.signer, `${SIGNER_PACKAGE}@${SIGNER_VERSION}`);
  assert.equal(metadata.signer_tarball, sha256(await readFile(SIGNER_TARBALL)));
  assert.equal(metadata.target, 'chrome116');
  assert.equal(metadata.determinism_verified, true);

  for (const filename of SCRIPT_FILES) {
    const bytes = await readFile(path.join(DIST, filename));
    assert.equal(metadata.outputs[filename], sha256(bytes));
    const source = bytes.toString('utf8');
    auditExecutableSource(filename, source);
    if (filename === 'background.js') {
      assert.match(source, /createChromeBrowserSigningProvider/);
      assert.match(source, /signer-state/);
      assert.match(source, /browser-signing-read\/v1/);
      assert.match(source, /active_account_partition_v4/);
      assert.doesNotMatch(source, /browser_signing_state_v2:/);
      assert.doesNotMatch(source, /bridge-clean-dev-ticket|DEV_AUTH_TICKET|DEV_ACCOUNT_ID/);
      assert.doesNotMatch(source, /dev-creator-account/);
    }
  }
  for (const filename of ICON_FILES) {
    const details = await stat(path.join(DIST, filename));
    assert.ok(details.size > 0, `${filename} is empty`);
    assert.equal(metadata.outputs[filename], sha256(await readFile(path.join(DIST, filename))));
  }
  const notice = await readFile(path.join(DIST, NOTICE_FILE), 'utf8');
  assert.match(notice, /local-authenticated-read-connector/);
  assert.match(notice, /MIT License/);
  assert.equal(metadata.outputs[NOTICE_FILE], sha256(TEXT_ENCODER.encode(notice)));
  assert.equal(
    metadata.outputs['manifest.json'],
    sha256(TEXT_ENCODER.encode(stableJson(manifest))),
  );
}

async function writeArtifact(compiled) {
  const sourceManifest = await readJson(path.join(ROOT, 'manifest.json'));
  auditManifest(sourceManifest);
  await rm(DIST, { force: true, recursive: true });
  await mkdir(path.join(DIST, 'icons'), { recursive: true });

  for (const [filename, bytes] of compiled) {
    await writeFile(path.join(DIST, filename), bytes);
  }
  const manifestBytes = TEXT_ENCODER.encode(stableJson(sourceManifest));
  await writeFile(path.join(DIST, 'manifest.json'), manifestBytes);
  for (const filename of ICON_FILES) {
    await copyFile(path.join(ROOT, filename), path.join(DIST, filename));
  }
  const signerLicense = await readFile(path.join(
    ROOT,
    'node_modules',
    SIGNER_PACKAGE,
    'LICENSE',
  ), 'utf8');
  const notice = [
    `${SIGNER_PACKAGE}@${SIGNER_VERSION}`,
    '',
    signerLicense.trim(),
    '',
  ].join('\n');
  await writeFile(path.join(DIST, NOTICE_FILE), notice, 'utf8');

  const outputs = {};
  for (const filename of [...SCRIPT_FILES, 'manifest.json', ...ICON_FILES, NOTICE_FILE]) {
    outputs[filename] = sha256(await readFile(path.join(DIST, filename)));
  }
  const metadata = {
    schema: 'ofca-extension-build/v1',
    extension_version: sourceManifest.version,
    signer: `${SIGNER_PACKAGE}@${SIGNER_VERSION}`,
    signer_tarball: sha256(await readFile(SIGNER_TARBALL)),
    target: 'chrome116',
    determinism_verified: true,
    outputs,
  };
  await writeFile(path.join(DIST, 'build-meta.json'), stableJson(metadata), 'utf8');
}

async function main() {
  if (process.argv.includes('--audit')) {
    await auditArtifact();
    process.stdout.write('Extension artifact audit passed.\n');
    return;
  }
  const first = await compileOnce();
  const second = await compileOnce();
  verifyIdenticalBuilds(first, second);
  await writeArtifact(first);
  await auditArtifact();
  process.stdout.write('Deterministic extension build and audit passed.\n');
}

await main();
