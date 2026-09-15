import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { auditReleaseArchive } from './artifact-contract.mjs';
import { extractAuditedArchive } from './archive-entries.mjs';
import { validateAcceptanceEvidence } from './acceptance-evidence.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_ROOT = path.dirname(ROOT);
const RELEASE_ROOT = path.join(EXTENSION_ROOT, 'dist', 'release');

function argumentValue(name, argv = process.argv.slice(2)) {
  const inline = argv.find((argument) => argument.startsWith(`${name}=`));
  if (inline !== undefined) return inline.slice(name.length + 1);
  const index = argv.indexOf(name);
  return index === -1 ? null : argv[index + 1] ?? null;
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

async function run(executable, args, { cwd = EXTENSION_ROOT, env = process.env, capture = false } = {}) {
  const child = spawn(executable, args, {
    cwd,
    env,
    shell: false,
    windowsHide: true,
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
  });
  let stdout = '';
  let stderr = '';
  if (capture) {
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  }
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (value, signal) => {
      if (signal !== null) reject(new Error(`${executable} terminated by ${signal}`));
      else resolve(value ?? 1);
    });
  });
  if (code !== 0) throw new Error(`${executable} exited ${code}${capture ? `: ${stderr}` : ''}`);
  return stdout.trim();
}

async function main() {
  const signingRule = argumentValue('--packaged-signing-rule');
  const legalBindings = argumentValue('--legal-release-bindings');
  const privacyPolicyUrl = argumentValue('--privacy-policy-url');
  const minimumChromium = argumentValue('--chromium-min');
  const currentChromium = argumentValue('--chromium-current');
  const acceptancePath = argumentValue('--acceptance-evidence');
  if (!signingRule || !legalBindings || !privacyPolicyUrl || !minimumChromium || !currentChromium) {
    throw new Error(
      'verify:release requires packaged signing rule, Legal release bindings, privacy policy URL, minimum Chrome 132 Chromium, and current Chromium',
    );
  }
  const npmCli = process.env.npm_execpath;
  if (!npmCli) throw new Error('verify:release must run through npm so the pinned npm CLI is known');

  const candidateRoot = await mkdtemp(path.join(os.tmpdir(), 'ofca-release-'));
  const buildDir = path.join(candidateRoot, 'build');
  const extractedDir = path.join(candidateRoot, 'artifact');
  const buildArgs = [
    path.join(EXTENSION_ROOT, 'build.mjs'),
    '--package',
    `--outdir=${buildDir}`,
    `--packaged-signing-rule=${path.resolve(signingRule)}`,
    `--legal-release-bindings=${path.resolve(legalBindings)}`,
    `--privacy-policy-url=${privacyPolicyUrl}`,
  ];
  let promoted = false;

  try {
    const sourceRevision = await run('git', ['rev-parse', 'HEAD'], { capture: true });
    assert.equal(await run('git', ['status', '--porcelain', '--untracked-files=no'], { capture: true }), '',
      'release verification requires a clean source revision');
    await run(process.execPath, [npmCli, 'test']);
    await run(process.execPath, [npmCli, 'run', 'check:architecture']);
    await run(process.execPath, buildArgs);

    const archives = (await readdir(buildDir)).filter((name) => name.endsWith('.zip'));
    assert.equal(archives.length, 1, 'release build must produce exactly one Chrome ZIP');
    const artifact = path.join(buildDir, archives[0]);
    const before = sha256(await readFile(artifact));

    await run(process.execPath, [
      path.join(EXTENSION_ROOT, 'build.mjs'),
      '--audit-package',
      `--artifact=${artifact}`,
      `--packaged-signing-rule=${path.resolve(signingRule)}`,
      `--legal-release-bindings=${path.resolve(legalBindings)}`,
    ]);
    const contract = await auditReleaseArchive({
      artifact,
      legalBindings: path.resolve(legalBindings),
    });
    assert.equal(contract.sha256, before);

    await extractAuditedArchive(await readFile(artifact), extractedDir, contract.archive_files);
    const browsers = {};
    for (const [label, executable] of [['minimum', minimumChromium], ['current', currentChromium]]) {
      const reportPath = path.join(candidateRoot, `${label}.json`);
      await run(process.execPath, [npmCli, 'run', 'test:browser:release'], { env: {
        ...process.env,
        EXTENSION_ARTIFACT_DIR: extractedDir,
        OFCA_CHROMIUM: path.resolve(executable),
        OFCA_BROWSER_LABEL: label,
        OFCA_BROWSER_REPORT: reportPath,
        OFCA_BROWSER_OUTPUT: path.join(candidateRoot, `browser-${label}`),
      } });
      browsers[label] = JSON.parse(await readFile(reportPath, 'utf8'));
      assert.equal(browsers[label].result, 'passed');
    }

    const after = sha256(await readFile(artifact));
    assert.equal(after, before, 'audited Chrome ZIP changed during browser qualification');
    assert.equal(await run('git', ['rev-parse', 'HEAD'], { capture: true }), sourceRevision);
    assert.equal(await run('git', ['status', '--porcelain', '--untracked-files=no'], { capture: true }), '');
    if (!acceptancePath) {
      throw new Error(`Browser smoke tests passed for sha256:${before}; promotion requires --acceptance-evidence with all production scenarios. Candidate: ${artifact}`);
    }
    const acceptanceBytes = await readFile(path.resolve(acceptancePath));
    validateAcceptanceEvidence(JSON.parse(acceptanceBytes.toString('utf8')), {
      artifactDigest: before,
      sourceRevision,
      currentMajor: Number(/(?:Chrome|Chromium)\/(\d+)/.exec(browsers.current.product)?.[1]),
    });

    await mkdir(RELEASE_ROOT, { recursive: true });
    const promotionDirectory = await mkdtemp(path.join(RELEASE_ROOT, '.candidate-'));
    const releaseDirectory = path.join(RELEASE_ROOT, before);
    const promotedArtifact = path.join(promotionDirectory, path.basename(artifact));
    await copyFile(artifact, promotedArtifact);
    assert.equal(sha256(await readFile(promotedArtifact)), before);

    const report = {
      schema: 'ofca-extension-release-verification/v1',
      source_revision: sourceRevision,
      artifact: path.basename(promotedArtifact),
      artifact_sha256: before,
      extension_version: contract.extension_version,
      minimum_chrome_version: contract.minimum_chrome_version,
      build_target: contract.target,
      legal_bindings_digest: contract.legal_bindings_digest,
      unit_tests: 'passed',
      architecture_check: 'passed',
      archive_audit: 'passed',
      browser_smoke_tests: browsers,
      production_acceptance: 'passed',
      acceptance_evidence_sha256: sha256(acceptanceBytes),
    };
    await writeFile(
      path.join(promotionDirectory, 'release-report.json'),
      `${JSON.stringify(report, null, 2)}\n`,
      'utf8',
    );
    await writeFile(path.join(promotionDirectory, 'acceptance-evidence.json'), acceptanceBytes);
    await rename(promotionDirectory, releaseDirectory);
    promoted = true;
    process.stdout.write(`Release verification passed: ${releaseDirectory} (sha256:${before})\n`);
  } finally {
    if (promoted) await rm(candidateRoot, { force: true, recursive: true });
    else process.stderr.write(`Unpromoted candidate retained for investigation: ${candidateRoot}\n`);
  }
}

await main();
