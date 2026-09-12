import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const repository = path.resolve(root, '../../..');
const destination = path.resolve(root, '../../vendor/companion-snow');
const checking = process.argv.includes('--check');
const output = checking ? await mkdtemp(path.join(os.tmpdir(), 'ofca-snow-check-')) : destination;
const target = checking ? path.join(output, 'target') : path.join(root, 'target');
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const run = (command, args) => execFileSync(command, args, {
  cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
  env: {
    ...process.env,
    CARGO_INCREMENTAL: '0',
    CARGO_TARGET_DIR: target,
    SOURCE_DATE_EPOCH: '0',
    RUSTFLAGS: `--remap-path-prefix=${repository}=/src --remap-path-prefix=${process.env.CARGO_HOME ?? path.join(os.homedir(), '.cargo')}=/cargo`,
  },
});

assert.match(run('rustc', ['--version']), /^rustc 1\.98\.1 /);
assert.equal(run('wasm-bindgen', ['--version']).trim(), 'wasm-bindgen 0.2.128');
run('cargo', ['build', '--locked', '--release', '--target', 'wasm32-unknown-unknown']);
await mkdir(output, { recursive: true });
run('wasm-bindgen', [
  '--target', 'web', '--no-typescript', '--out-dir', output,
  path.join(target, 'wasm32-unknown-unknown/release/ofca_snow_wasm.wasm'),
]);
await writeFile(path.join(output, 'package.json'), '{"type":"module"}\n');

const metadata = JSON.parse(run('cargo', [
  'metadata', '--format-version', '1', '--locked', '--filter-platform', 'wasm32-unknown-unknown',
]));
const used = new Set(metadata.resolve.nodes.map((node) => node.id));
const dependencies = metadata.packages.filter((pkg) => used.has(pkg.id) && pkg.source)
  .sort((a, b) => a.name.localeCompare(b.name));
const notices = [];
for (const pkg of dependencies) {
  const folder = path.dirname(pkg.manifest_path);
  const names = (await readdir(folder)).filter((name) => /^(?:licen[sc]e|copying|notice)(?:[._-]|$)/i.test(name)).sort();
  assert.ok(names.length > 0, `upstream license absent for ${pkg.name}`);
  notices.push(`${pkg.name} ${pkg.version} (${pkg.license})`);
  for (const name of names) notices.push(`${name}\n${(await readFile(path.join(folder, name), 'utf8')).trim()}`);
}
await writeFile(path.join(output, 'THIRD_PARTY_NOTICES.txt'), `${notices.join('\n\n')}\n`);

const inputs = {};
for (const file of ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'src/lib.rs', 'build.mjs']) {
  inputs[file] = sha(await readFile(path.join(root, file)));
}
const outputs = {};
for (const file of ['ofca_snow_wasm.js', 'ofca_snow_wasm_bg.wasm', 'package.json', 'THIRD_PARTY_NOTICES.txt']) {
  outputs[file] = sha(await readFile(path.join(output, file)));
}
const release = {
  schema: 'ofca-companion-snow-wasm/v1',
  suite: 'Noise_KK_25519_ChaChaPoly_SHA256',
  snow: '0.10.0', rust: '1.98.1', wasm_bindgen: '0.2.128', target: 'wasm32-unknown-unknown',
  inputs, outputs,
  dependencies: dependencies.map(({ name, version, source, license }) => ({ name, version, source, license })),
};
await writeFile(path.join(output, 'release.json'), `${JSON.stringify(release, null, 2)}\n`);
if (checking) {
  for (const file of [...Object.keys(outputs), 'release.json']) {
    assert.equal(sha(await readFile(path.join(output, file))), sha(await readFile(path.join(destination, file))), `Snow rebuild differs: ${file}`);
  }
}
process.stdout.write(`${checking ? 'Verified reproducible' : 'Built'} packaged Snow WASM ${outputs['ofca_snow_wasm_bg.wasm']}\n`);
