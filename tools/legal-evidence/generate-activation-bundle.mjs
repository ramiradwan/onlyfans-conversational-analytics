import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  copyFile,
  mkdir,
  readFile,
  stat,
  writeFile,
} from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ACTIVATION_SCENARIO_REGISTRY } from './activation-scenarios.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const LOCK_PATH = path.join(ROOT, 'shared/legal/activation-evidence.lock.json');
const SCHEMA_PATH = path.join(ROOT, 'shared/legal/activation-evidence.schema.json');
const FIXTURE_PATH = path.join(
  ROOT,
  'extension/tests/fixtures/legal-instrument-bindings.synthetic.json',
);

function argument(name) {
  const prefix = `${name}=`;
  const inline = process.argv.find((entry) => entry.startsWith(prefix));
  if (inline) return inline.slice(prefix.length);
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1] ?? null;
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function gitBlobSha(bytes) {
  return createHash('sha1')
    .update(Buffer.from(`blob ${bytes.length}\0`))
    .update(bytes)
    .digest('hex');
}

function json(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

async function exists(filename) {
  try {
    await stat(filename);
    return true;
  } catch (_error) {
    return false;
  }
}

async function copyRelative(sourceRoot, destinationRoot, relativePath) {
  const source = path.join(sourceRoot, relativePath);
  if (!await exists(source)) throw new Error(`Required executed evidence is missing: ${relativePath}`);
  const destination = path.join(destinationRoot, relativePath);
  await mkdir(path.dirname(destination), { recursive: true });
  await copyFile(source, destination);
  return relativePath.replaceAll('\\', '/');
}

const checkedOutRevision = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: ROOT,
  encoding: 'utf8',
}).trim();
const revision = process.env.OFCA_PRODUCT_REVISION
  ?? process.env.PRODUCT_REVISION
  ?? checkedOutRevision;
assert.match(revision, /^[a-f0-9]{40}$/);
assert.equal(
  revision,
  checkedOutRevision,
  'Declared Product revision does not match the checked-out revision',
);

const executionRootArgument = argument('--execution-root')
  ?? process.env.OFCA_LEGAL_EVIDENCE_EXECUTION_ROOT;
if (!executionRootArgument) {
  throw new Error('Legal evidence packager requires --execution-root or OFCA_LEGAL_EVIDENCE_EXECUTION_ROOT');
}
const executionRoot = path.resolve(executionRootArgument);
const runtimeProofArgument = argument('--runtime-proof')
  ?? process.env.OFCA_LEGAL_RUNTIME_PROOF_PATH
  ?? null;
const allowIncomplete = process.argv.includes('--allow-incomplete');
const outputRoot = path.resolve(
  argument('--output') ?? path.join(ROOT, 'artifacts/legal/activation-v2', revision),
);
await mkdir(outputRoot, { recursive: true });

const lock = JSON.parse(await readFile(LOCK_PATH, 'utf8'));
const schemaBytes = await readFile(SCHEMA_PATH);
assert.equal(lock.schema_version, '2.0');
assert.equal(lock.source_blob_sha, 'aa550690ae5dbf840826b74864a5fbd0290ab441');
assert.equal(sha256(schemaBytes), lock.source_sha256);
assert.equal(gitBlobSha(schemaBytes), lock.source_blob_sha);

const copiedFiles = [];
const scenarioManifest = {};
for (const scenario of ACTIVATION_SCENARIO_REGISTRY) {
  const resultPath = path.join(executionRoot, scenario.result_file);
  if (!await exists(resultPath)) {
    throw new Error(`${scenario.id} has no preserved executed result`);
  }
  const result = JSON.parse(await readFile(resultPath, 'utf8'));
  assert.equal(result.schema, 'ofca-product-legal-scenario-result/v1');
  assert.equal(result.product_revision, revision);
  assert.equal(result.scenario_id, scenario.id);
  assert.equal(result.product_test_status, 'PASS');
  assert.equal(result.legal_acceptance_status, 'UNSCORED');
  assert.equal(result.executing_test, scenario.test_name);
  assert.deepEqual(result.evidence_files, scenario.evidence_files);

  copiedFiles.push(await copyRelative(executionRoot, outputRoot, scenario.result_file));
  for (const evidenceFile of scenario.evidence_files) {
    const evidencePath = path.join(executionRoot, evidenceFile);
    const evidence = JSON.parse(await readFile(evidencePath, 'utf8'));
    assert.equal(evidence.schema, 'ofca-product-legal-scenario-evidence/v1');
    assert.equal(evidence.product_revision, revision);
    assert.equal(evidence.scenario_id, scenario.id);
    assert.equal(evidence.synthetic_instrument_bindings, true);
    copiedFiles.push(await copyRelative(executionRoot, outputRoot, evidenceFile));
  }

  scenarioManifest[scenario.id] = {
    product_test_status: 'PASS',
    legal_acceptance_status: 'UNSCORED',
    executing_test: scenario.test_name,
    result_file: scenario.result_file,
    evidence_files: scenario.evidence_files,
    runtime_proof_required: scenario.runtime_proof === true,
  };
}

const testOutputFiles = [
  'test-results/activation-evidence.tap',
  'test-results/browser-e2e.txt',
];
for (const relativePath of testOutputFiles) {
  copiedFiles.push(await copyRelative(executionRoot, outputRoot, relativePath));
}

let runtimeProof = null;
if (runtimeProofArgument !== null && await exists(path.resolve(runtimeProofArgument))) {
  runtimeProof = JSON.parse(await readFile(path.resolve(runtimeProofArgument), 'utf8'));
  assert.equal(runtimeProof.schema, 'ofca-legal-activation-runtime-proof/v1');
  assert.equal(runtimeProof.product_revision, revision);
  assert.equal(runtimeProof.synthetic_instrument_bindings, true);
  assert.equal(runtimeProof.preview_record_preserved, true);
  assert.equal(runtimeProof.distinct_mode_upgrade_event, true);
  assert.deepEqual(runtimeProof.user_flow, [
    'Terms acceptance',
    'Risk acknowledgment',
    'Activate Software',
    'Enable Preview',
    'Review Full analytics',
    'Accepted Full prominent disclosure',
    'Enable Full analytics',
  ]);
  const runtimeRelative = 'runtime/runtime-ui-proof.json';
  await mkdir(path.join(outputRoot, 'runtime'), { recursive: true });
  await writeFile(path.join(outputRoot, runtimeRelative), json(runtimeProof), 'utf8');
  copiedFiles.push(runtimeRelative);
} else if (!allowIncomplete) {
  throw new Error('Exact-revision runtime/UI proof is required for a complete Legal evidence bundle');
}

const schemaFiles = [
  ['schema/activation-evidence.schema.json', SCHEMA_PATH],
  ['schema/activation-evidence.lock.json', LOCK_PATH],
  ['schema/synthetic-instrument-bindings.json', FIXTURE_PATH],
];
for (const [relativePath, source] of schemaFiles) {
  await mkdir(path.dirname(path.join(outputRoot, relativePath)), { recursive: true });
  await copyFile(source, path.join(outputRoot, relativePath));
  copiedFiles.push(relativePath);
}

const storageAuditRelative = 'storage-and-audit.md';
await writeFile(
  path.join(outputRoot, storageAuditRelative),
  `# Activation evidence storage and audit\n\n`
  + `- Product revision: \`${revision}\`\n`
  + `- Evidence database: \`ofca_legal_evidence_v1\` (installation-scoped IndexedDB).\n`
  + `- Records are append-only pre-mode Terms/risk actions plus schema-v2 mode envelopes.\n`
  + `- Active Preview/Full entry is authorized explicitly by ConsentController through the persisted Legal evidence policy.\n`
  + `- Retry uses deterministic record keys and persisted pending/completed mode intent; crash recovery reuses original event IDs and timestamps.\n`
  + `- Revocation changes current consent/permissions through the real ConsentController path without rewriting historical evidence.\n`
  + `- Legacy active consent without corresponding evidence is paused during reconciliation and requires a Legal mode choice.\n`
  + `- Audit export returns the chronological journal.\n`
  + `- Delete all Extension data deletes this Extension-owned evidence database; it does not delete companion-service data.\n`
  + `- All instrument bindings in this implementation evidence bundle are synthetic. Production bindings remain fail-closed until Legal-controlled release metadata is supplied.\n`
  + `- Product execution status is not Legal acceptance. Legal alone assigns MATCH, MISMATCH, or INCOMPLETE.\n`,
  'utf8',
);
copiedFiles.push(storageAuditRelative);

const fileHashes = {};
for (const relativePath of [...new Set(copiedFiles)].sort()) {
  fileHashes[relativePath] = sha256(await readFile(path.join(outputRoot, relativePath)));
}

const manifest = {
  schema: 'ofca-legal-activation-evidence-bundle/v2',
  product_revision: revision,
  product_evidence_status: runtimeProof === null ? 'INCOMPLETE_RUNTIME_PROOF' : 'COMPLETE',
  legal_acceptance_status: 'UNSCORED',
  synthetic_instrument_bindings: true,
  production_legal_bindings_supplied: false,
  legal_schema_lock: lock,
  scenarios: scenarioManifest,
  runtime_proof: runtimeProof === null ? null : 'runtime/runtime-ui-proof.json',
  test_outputs: testOutputFiles,
  file_sha256: fileHashes,
  engineering_attestation: 'Generated separately after the final signed Product revision reaches the attestation stage.',
};
await writeFile(path.join(outputRoot, 'manifest.json'), json(manifest), 'utf8');
console.log(outputRoot);
