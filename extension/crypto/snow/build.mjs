import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { compareCodePoints, normalizeNotice, sourcePathMappings } from './build-inputs.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const destination = path.resolve(root, '../../vendor/companion-snow');
const checking = process.argv.includes('--check');
const output = checking ? await mkdtemp(path.join(os.tmpdir(), 'ofca-snow-check-')) : destination;
const target = checking ? path.join(output, 'target') : path.join(root, 'target');
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
let rustFlags = [];
const run = (command, args) => execFileSync(command, args, {
  cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
  env: {
    ...process.env,
    CARGO_INCREMENTAL: '0',
    CARGO_TARGET_DIR: target,
    SOURCE_DATE_EPOCH: '0',
    RUSTFLAGS: '',
    CARGO_ENCODED_RUSTFLAGS: rustFlags.join('\x1f'),
  },
});

assert.match(run('rustc', ['--version']), /^rustc 1\.98\.1 /);
assert.equal(run('wasm-bindgen', ['--version']).trim(), 'wasm-bindgen 0.2.128');
const metadata = JSON.parse(run('cargo', [
  'metadata', '--format-version', '1', '--locked', '--filter-platform', 'wasm32-unknown-unknown',
]));
const used = new Set(metadata.resolve.nodes.map((node) => node.id));
const packages = metadata.packages.filter((pkg) => used.has(pkg.id))
  .sort((a, b) => compareCodePoints(`${a.name}@${a.version}`, `${b.name}@${b.version}`));
async function rustSources(folder) {
  const result = [];
  for (const item of await readdir(folder, { withFileTypes: true })) {
    if (item.isDirectory() && !['target', '.git'].includes(item.name)) result.push(...await rustSources(path.join(folder, item.name)));
    else if (item.isFile() && item.name.endsWith('.rs')) result.push(path.join(folder, item.name));
  }
  return result.sort(compareCodePoints);
}
const mappings = [];
for (const pkg of packages) {
  const folder = path.dirname(pkg.manifest_path);
  mappings.push(...sourcePathMappings(folder, pkg.name, pkg.version, await rustSources(folder), { local: pkg.source === null }));
}
await mkdir(target, { recursive: true });
const mappingIdentity = sha([...new Set(mappings.map(([, target]) => target))].sort(compareCodePoints).join('\n'));
const responseFile = path.join(target, `canonical-source-paths-${mappingIdentity}.rsp`);
await writeFile(responseFile, mappings.map(([from, to]) => `--remap-path-prefix=${from}=${to}`).join('\n') + '\n');
// A response file preserves paths containing spaces and avoids the Windows
// environment/command-line size limits without dropping any source remapping.
rustFlags = [`@${responseFile}`];
run('cargo', ['build', '--locked', '--release', '--target', 'wasm32-unknown-unknown']);
await mkdir(output, { recursive: true });
run('wasm-bindgen', [
  '--target', 'web', '--no-typescript', '--remove-name-section', '--remove-producers-section', '--out-dir', output,
  path.join(target, 'wasm32-unknown-unknown/release/ofca_snow_wasm.wasm'),
]);
await writeFile(path.join(output, 'package.json'), '{"type":"module"}\n');

const dependencies = packages.filter((pkg) => pkg.source);
const notices = [];
for (const pkg of dependencies) {
  const folder = path.dirname(pkg.manifest_path);
  const names = (await readdir(folder)).filter((name) => /^(?:licen[sc]e|copying|notice)(?:[._-]|$)/i.test(name)).sort();
  assert.ok(names.length > 0, `upstream license absent for ${pkg.name}`);
  notices.push(`${pkg.name} ${pkg.version} (${pkg.license})`);
  for (const name of names) notices.push(`${name}\n${normalizeNotice(await readFile(path.join(folder, name), 'utf8'))}`);
}
await writeFile(path.join(output, 'THIRD_PARTY_NOTICES.txt'), `${notices.join('\n\n')}\n`);

const inputs = {};
for (const file of ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'src/lib.rs', 'build.mjs', 'build-inputs.mjs']) {
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
  source_paths: 'complete-filenames/v1', debug_sections: false,
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
