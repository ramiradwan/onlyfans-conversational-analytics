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
import { unzipSync, zipSync } from 'fflate';
import { validatePackagedSigningRule } from 'local-authenticated-read-connector/browser-signing';

import {
  LEGAL_INSTRUMENT_BINDINGS_SCHEMA,
  LEGAL_INSTRUMENT_NAMES,
  validateLegalInstrumentBindings,
} from './runtime/legal-instruments.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.join(ROOT, 'dist');
const SIGNER_PACKAGE = 'local-authenticated-read-connector';
const SIGNER_VERSION = '0.2.0-beta.11';
const SIGNER_SPEC =
  'file:vendor/local-authenticated-read-connector-0.2.0-beta.11.tgz';
const SIGNER_TARBALL = path.join(
  ROOT,
  'vendor',
  'local-authenticated-read-connector-0.2.0-beta.11.tgz',
);
const SIGNER_ENTRY = fileURLToPath(import.meta.resolve(`${SIGNER_PACKAGE}/browser-signing`));
const SIGNING_RULE_FILE = 'packaged-signing-rule.json';
const BUILD_METADATA_FILE = 'build-meta.json';
const EXPECTED_EXTENSION_ID = 'mldllkjpnnjhdccpofhebhlhigpefcba';
const READ_ONLY_CAPABILITIES = Object.freeze([
  'capture.chats',
  'capture.messages',
  'capture.presence',
  'history.sync',
]);
const EXPECTED_PERMISSIONS = Object.freeze([
  'alarms',
  'scripting',
  'storage',
  'unlimitedStorage',
]);
const EXPECTED_OPTIONAL_PERMISSIONS = Object.freeze(['webRequest']);
const EXPECTED_OPTIONAL_HOST_PERMISSIONS = Object.freeze([
  'https://onlyfans.com/*',
  'http://bridge.localhost:17871/*',
]);
const EXPECTED_EXTERNAL_MATCHES = Object.freeze(['http://bridge.localhost:17871/*']);
const EXPECTED_EXTENSION_CSP = "script-src 'self'; object-src 'self'; connect-src 'self' http://bridge.localhost:17871 ws://bridge.localhost:17871;";
const FORBIDDEN_PERMISSIONS = Object.freeze([
  'cookies',
  'debugger',
  'nativeMessaging',
  'offscreen',
  'tabs',
  'webRequestBlocking',
]);
const BUNDLED_SCRIPT_FILES = Object.freeze([
  'background.js',
  'content.js',
  'page-hook.js',
  'popup.js',
]);
const MODE_SCRIPT_FILES = Object.freeze([
  'page-hook-mode-identity.js',
  'page-hook-mode-preview.js',
  'page-hook-mode-full.js',
]);
const SCRIPT_FILES = Object.freeze([...BUNDLED_SCRIPT_FILES, ...MODE_SCRIPT_FILES]);
const NOTICE_FILE = 'THIRD_PARTY_NOTICES.txt';
const ICON_FILES = Object.freeze(['icons/icon48.png', 'icons/icon128.png']);
const STATIC_UI_FILES = Object.freeze(['popup.html', 'popup.css']);
const UI_FILES = Object.freeze([...STATIC_UI_FILES, 'extension-config.json']);
const TEXT_ENCODER = new TextEncoder();
const TEXT_DECODER = new TextDecoder();
const ZIP_TIMESTAMP = new Date('1980-01-01T00:00:00.000Z');

function sha256(value) {
  return `sha256:${createHash('sha256').update(value).digest('hex')}`;
}

function sha512Integrity(value) {
  return `sha512-${createHash('sha512').update(value).digest('base64')}`;
}

function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

export function deriveExtensionId(manifestKey) {
  if (typeof manifestKey !== 'string' || manifestKey.length === 0) {
    throw new TypeError('manifest.key must contain a base64-encoded public key');
  }
  const digest = createHash('sha256').update(Buffer.from(manifestKey, 'base64')).digest();
  const alphabet = 'abcdefghijklmnop';
  return [...digest.subarray(0, 16)]
    .map((byte) => `${alphabet[byte >>> 4]}${alphabet[byte & 0x0f]}`)
    .join('');
}

function outputBytes(result, filename) {
  const suffix = `/${filename}`;
  const output = result.outputFiles.find((candidate) => (
    candidate.path.replaceAll('\\', '/').endsWith(suffix)
  ));
  if (!output) throw new Error(`esbuild did not produce ${filename}`);
  return output.contents;
}

function argumentValue(name) {
  const prefix = `${name}=`;
  const inline = process.argv.find((argument) => argument.startsWith(prefix));
  if (inline !== undefined) return inline.slice(prefix.length);
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1] ?? null;
}

async function readJson(filename) {
  return JSON.parse(await readFile(filename, 'utf8'));
}

export function validateExtensionConfig(document, { requirePrivacyPolicy = false } = {}) {
  assert.deepEqual(
    Object.keys(document).sort(),
    ['dashboard_url', 'history_settings_url', 'privacy_policy_url', 'schema'],
    'extension configuration contains unexpected fields',
  );
  assert.equal(document.schema, 'ofca-extension-config/v1');
  assert.equal(document.dashboard_url, 'http://bridge.localhost:17871/');
  assert.equal(document.history_settings_url, 'http://bridge.localhost:17871/settings');
  assert.equal(typeof document.privacy_policy_url, 'string');
  if (document.privacy_policy_url === '') {
    if (requirePrivacyPolicy) throw new Error('Chrome package requires a privacy policy URL.');
    return Object.freeze({ ...document });
  }
  const privacyUrl = new URL(document.privacy_policy_url);
  assert.equal(privacyUrl.protocol, 'https:', 'privacy policy URL must use HTTPS');
  assert.equal(privacyUrl.username, '', 'privacy policy URL must not contain credentials');
  assert.equal(privacyUrl.password, '', 'privacy policy URL must not contain credentials');
  assert.equal(privacyUrl.hostname.endsWith('.invalid'), false, 'privacy policy URL is invalid');
  return Object.freeze({ ...document, privacy_policy_url: privacyUrl.href });
}

async function loadExtensionConfig({ requirePrivacyPolicy }) {
  const document = await readJson(path.join(ROOT, 'extension-config.json'));
  const privacyPolicyArgument = argumentValue('--privacy-policy-url');
  const effective = privacyPolicyArgument === null
    ? document
    : { ...document, privacy_policy_url: privacyPolicyArgument };
  return validateExtensionConfig(effective, { requirePrivacyPolicy });
}

export function validateSigningRuleDocument(document) {
  return validatePackagedSigningRule(document);
}

async function loadSigningRule({ required }) {
  const filename = argumentValue('--packaged-signing-rule');
  if (filename === null || filename.length === 0) {
    if (required) {
      throw new Error(
        'Chrome package creation requires --packaged-signing-rule=<path>.',
      );
    }
    return null;
  }
  const rule = validateSigningRuleDocument(await readJson(path.resolve(filename)));
  const bytes = TEXT_ENCODER.encode(stableJson(rule));
  return Object.freeze({
    document: rule,
    bytes,
    digest: sha256(bytes),
  });
}

const LEGAL_RELEASE_BINDINGS_ARGUMENT = '--legal-release-bindings';

const LEGAL_RELEASE_BINDINGS_RULE = 'ADR 0022: a production package must not be created '
  + 'without valid Legal release bindings.';

export function validateLegalReleaseBindingsDocument(document) {
  return validateLegalInstrumentBindings(document);
}

/**
 * Resolve the Legal instrument bindings named on the command line.
 *
 * Only a production package requires them; the Extension already fails closed
 * without a binding. A supplied document is validated by the runtime validator
 * in every mode, and the build verifies it without embedding it.
 */
async function verifyLegalReleaseBindings({ required }) {
  const filename = argumentValue(LEGAL_RELEASE_BINDINGS_ARGUMENT);
  if (filename === null || filename.length === 0) {
    if (required) {
      throw new Error(
        `${LEGAL_RELEASE_BINDINGS_RULE} Supply `
        + `${LEGAL_RELEASE_BINDINGS_ARGUMENT}=<path> naming an `
        + `${LEGAL_INSTRUMENT_BINDINGS_SCHEMA} document that carries the controlling `
        + 'Legal revision, the activation schema blob, the HTTPS public origin, and the '
        + 'version, rendered SHA-256, public route and locale of '
        + `${LEGAL_INSTRUMENT_NAMES.join(', ')}.`,
      );
    }
    return null;
  }
  const resolved = path.resolve(filename);
  let document;
  try {
    document = await readJson(resolved);
  } catch (error) {
    throw new Error(`Legal release bindings are unreadable at ${resolved}: ${error.message}`);
  }
  try {
    return validateLegalReleaseBindingsDocument(document);
  } catch (error) {
    const prefix = required ? `${LEGAL_RELEASE_BINDINGS_RULE} ` : '';
    throw new Error(
      `${prefix}Legal release bindings at ${resolved} are invalid: ${error.message}`,
    );
  }
}

export function signerWrapperSource(signingRule) {
  const ruleBase64 = signingRule === null
    ? ''
    : Buffer.from(signingRule.bytes).toString('base64');
  const ruleDigest = signingRule?.digest ?? '';
  return [
    "import * as signerModule from 'packaged-signer-implementation';",
    "export * from 'packaged-signer-implementation';",
    `export const PACKAGED_SIGNING_RULE_B64 = ${JSON.stringify(ruleBase64)};`,
    `export const PACKAGED_SIGNING_RULE_SHA256 = ${JSON.stringify(ruleDigest)};`,
    'const PACKAGED_SIGNING_RULE = PACKAGED_SIGNING_RULE_B64 === \'\'',
    '  ? null',
    '  : Object.freeze(JSON.parse(atob(PACKAGED_SIGNING_RULE_B64)));',
    'export async function createChromeBrowserSigningProvider(options = {}) {',
    '  if (PACKAGED_SIGNING_RULE === null || PACKAGED_SIGNING_RULE_SHA256 === \'\') {',
    "    const error = new Error('History sync is unavailable for this revision; update the extension.');",
    "    error.code = 'unsupported_revision';",
    '    throw error;',
    '  }',
    '  return signerModule.createChromeBrowserSigningProvider({',
    '    ...options,',
    '    packagedRule: PACKAGED_SIGNING_RULE,',
    '  });',
    '}',
    '',
  ].join('\n');
}

function packagedSignerPlugin(signingRule) {
  return {
    name: 'packaged-signer-rule',
    setup(context) {
      context.onResolve(
        { filter: /^local-authenticated-read-connector\/browser-signing$/ },
        () => ({ path: 'packaged-signer-rule', namespace: 'packaged-signer-rule' }),
      );
      context.onResolve(
        { filter: /^packaged-signer-implementation$/, namespace: 'packaged-signer-rule' },
        () => ({ path: SIGNER_ENTRY }),
      );
      context.onLoad(
        { filter: /.*/, namespace: 'packaged-signer-rule' },
        () => ({ contents: signerWrapperSource(signingRule), loader: 'js' }),
      );
    },
  };
}

const FORBIDDEN_BACKGROUND_INPUTS = Object.freeze([
  /(^|\/)background\.js$/,
  /(^|\/)protocol\/(?:index|types|validation)\.mjs$/,
  /(^|\/)transport\/agent-command-service\.mjs$/,
  /(^|\/)transport\/agent-config-client\.mjs$/,
  /(^|\/)transport\/agent-runtime\.mjs$/,
  /(^|\/)transport\/agent-websocket\.mjs$/,
  /(^|\/)transport\/capture-ingestion\.mjs$/,
  /(^|\/)transport\/chrome-adapter\.mjs$/,
  /(^|\/)transport\/config-http-adapter\.mjs$/,
  /(^|\/)transport\/durable-outbox\.mjs$/,
  /(^|\/)transport\/history-coordinator\.mjs$/,
  /(^|\/)transport\/indexeddb-ingestion-storage\.mjs$/,
  /(^|\/)transport\/signer-normalization\.mjs$/,
  /(^|\/)(?:qa|qualification|tests|tools)\//,
]);

export function auditReadOnlyModuleGraph(inputNames) {
  const normalized = inputNames.map((input) => input.replaceAll('\\', '/'));
  assert.equal(
    normalized.some((input) => /(^|\/)background-read-only\.js$/.test(input)),
    true,
    'background build does not use the read-only entry',
  );
  for (const input of normalized) {
    for (const forbidden of FORBIDDEN_BACKGROUND_INPUTS) {
      assert.equal(
        forbidden.test(input),
        false,
        `read-only background includes forbidden module: ${input}`,
      );
    }
  }
  return Object.freeze([...normalized].sort());
}

async function compileOnce(signingRule) {
  const common = {
    bundle: true,
    charset: 'utf8',
    legalComments: 'none',
    logLevel: 'silent',
    metafile: true,
    minify: false,
    platform: 'browser',
    plugins: [packagedSignerPlugin(signingRule)],
    sourcemap: false,
    target: ['chrome116'],
    treeShaking: true,
    write: false,
  };
  const [background, content, pageHook, popup] = await Promise.all([
    build({
      ...common,
      entryPoints: [path.join(ROOT, 'background-read-only.js')],
      format: 'esm',
      outfile: path.join(DIST, 'background.js'),
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
    build({
      ...common,
      entryPoints: [path.join(ROOT, 'popup.js')],
      format: 'iife',
      outfile: path.join(DIST, 'popup.js'),
    }),
  ]);

  auditReadOnlyModuleGraph(Object.keys(background.metafile.inputs));

  const signerInputs = Object.keys(background.metafile.inputs)
    .map((input) => input.replaceAll('\\', '/'))
    .filter((input) => input.includes(`${SIGNER_PACKAGE}/src/signing/`));
  assert.ok(signerInputs.length > 0, 'background bundle did not include the local signer package');

  return new Map([
    ['background.js', outputBytes(background, 'background.js')],
    ['content.js', outputBytes(content, 'content.js')],
    ['page-hook.js', outputBytes(pageHook, 'page-hook.js')],
    ['popup.js', outputBytes(popup, 'popup.js')],
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

function auditManifest(manifest) {
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.minimum_chrome_version, '116');
  assert.equal(manifest.name, 'Conversation Analytics');
  assert.equal(deriveExtensionId(manifest.key), EXPECTED_EXTENSION_ID);
  assert.deepEqual(manifest.permissions, EXPECTED_PERMISSIONS);
  assert.deepEqual(manifest.optional_permissions, EXPECTED_OPTIONAL_PERMISSIONS);
  assert.equal(manifest.host_permissions, undefined);
  assert.deepEqual(manifest.optional_host_permissions, EXPECTED_OPTIONAL_HOST_PERMISSIONS);
  assert.deepEqual(manifest.externally_connectable?.matches, EXPECTED_EXTERNAL_MATCHES);
  assert.equal(manifest.background?.service_worker, 'background.js');
  assert.equal(manifest.background?.type, 'module');
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.action?.default_popup, 'popup.html');
  const declared = new Set([
    ...(manifest.permissions ?? []),
    ...(manifest.optional_permissions ?? []),
  ]);
  for (const permission of FORBIDDEN_PERMISSIONS) {
    assert.equal(declared.has(permission), false, `forbidden permission declared: ${permission}`);
  }
  const policy = manifest.content_security_policy?.extension_pages ?? '';
  assert.equal(policy, EXPECTED_EXTENSION_CSP);
  assert.equal(policy.includes("script-src 'self'"), true);
  assert.equal(policy.includes("object-src 'self'"), true);
  assert.equal(policy.includes("'unsafe-eval'"), false);
}

const FORBIDDEN_EXECUTABLE_PATTERNS = Object.freeze([
  Object.freeze({ name: 'eval', expression: /\beval\s*\(/ }),
  Object.freeze({ name: 'Function constructor', expression: /\b(?:new\s+)?Function\s*\(/ }),
  Object.freeze({ name: 'Function source inspection', expression: /Function\.prototype\.toString/ }),
  Object.freeze({ name: 'runtime discovery', expression: /runtime[-_ ]?discovery/i }),
  Object.freeze({ name: 'runtime inspection', expression: /inspectSigningRuntime|discoverSigningRulesInPage|deriveSigningGeneration/ }),
  Object.freeze({ name: 'webpack runtime', expression: /webpackChunk|__webpack_require__/ }),
  Object.freeze({ name: 'module factory execution', expression: /module[-_ ]?factor(?:y|ies)|executeModuleFactory/i }),
  Object.freeze({ name: 'remote importScripts', expression: /\bimportScripts\s*\(\s*['"`]https?:\/\//i }),
  Object.freeze({ name: 'remote dynamic import', expression: /\bimport\s*\(\s*['"`]https?:\/\//i }),
  Object.freeze({ name: 'remote static import', expression: /\bfrom\s*['"`]https?:\/\//i }),
  Object.freeze({ name: 'remote signing rule', expression: /(?:remote|download|fetch|load)[A-Za-z0-9_$]*(?:signing|signer)[A-Za-z0-9_$]*(?:rule|logic|code)/i }),
  Object.freeze({ name: 'signing rule URL', expression: /(?:signing|signer)[A-Za-z0-9_$]*(?:rule|logic|code)[A-Za-z0-9_$]*url/i }),
  Object.freeze({ name: 'signing rule fallback', expression: /(?:fallback[A-Za-z0-9_$]*(?:signing|signer)|(?:signing|signer)[A-Za-z0-9_$]*fallback)/i }),
]);
const FORBIDDEN_READ_ONLY_PATTERNS = Object.freeze([
  Object.freeze({ name: 'command execution frame', expression: /['"`]command\.execute['"`]/ }),
  Object.freeze({ name: 'command result frame', expression: /['"`]command\.result(?:\.ack)?['"`]/ }),
  Object.freeze({ name: 'command capability', expression: /['"`]command\.message\.send['"`]/ }),
  Object.freeze({ name: 'message mutation action', expression: /['"`]message\.send['"`]/ }),
  Object.freeze({ name: 'page mutation channel', expression: /_OF_BACKEND_|send_ws_message|send_fetch_command/i }),
]);

export function auditExecutableSource(filename, source) {
  for (const { name, expression } of FORBIDDEN_EXECUTABLE_PATTERNS) {
    assert.equal(
      expression.test(source),
      false,
      `${filename} contains forbidden ${name}`,
    );
  }
}

export function auditReadOnlyExecutable(filename, source) {
  for (const { name, expression } of FORBIDDEN_READ_ONLY_PATTERNS) {
    assert.equal(expression.test(source), false, `${filename} contains forbidden ${name}`);
  }
}

export function auditReadOnlyBackground(source) {
  auditReadOnlyExecutable('background.js', source);
  const capabilityList = READ_ONLY_CAPABILITIES
    .map((capability) => `\\s*["']${capability.replaceAll('.', '\\.')}["']\\s*`)
    .join(',');
  assert.match(
    source,
    new RegExp(`READ_ONLY_CAPABILITIES\\s*=\\s*Object\\.freeze\\(\\[${capabilityList}\\]\\)`),
    'background.js does not bind the exact read-only capability set',
  );
  assert.match(source, /capabilities:\s*READ_ONLY_CAPABILITIES/);
  assert.match(source, /capabilities:\s*this\.capabilities/);
}

function occurrences(source, value) {
  if (value.length === 0) return 0;
  return source.split(value).length - 1;
}

export function auditSigningRuleBinding(source, signingRule, { required }) {
  assert.match(source, /PACKAGED_SIGNING_RULE_B64/);
  assert.match(source, /PACKAGED_SIGNING_RULE_SHA256/);
  assert.match(source, /packagedRule:\s*PACKAGED_SIGNING_RULE/);
  assert.doesNotMatch(source, /packagedRule\s*\?\?|packagedRule\s*\|\|/);
  assert.match(source, /unsupported_revision/);
  assert.match(source, /update the extension/i);

  if (signingRule === null) {
    assert.equal(required, false, 'packaged Chrome artifact has no signing rule');
    assert.match(source, /PACKAGED_SIGNING_RULE_B64\s*=\s*""/);
    assert.match(source, /PACKAGED_SIGNING_RULE_SHA256\s*=\s*""/);
    return;
  }

  const encoded = Buffer.from(signingRule.bytes).toString('base64');
  assert.equal(occurrences(source, encoded), 1, 'packaged signing rule is not bound exactly once');
  assert.equal(
    occurrences(source, signingRule.digest),
    1,
    'packaged signing rule digest is not bound exactly once',
  );
  assert.match(source, new RegExp(`PACKAGED_SIGNING_RULE_B64\\s*=\\s*${JSON.stringify(encoded)}`));
  assert.match(
    source,
    new RegExp(`PACKAGED_SIGNING_RULE_SHA256\\s*=\\s*${JSON.stringify(signingRule.digest)}`),
  );
}

async function auditDependencyLock() {
  const packageDocument = await readJson(path.join(ROOT, 'package.json'));
  assert.equal(packageDocument.dependencies?.[SIGNER_PACKAGE], SIGNER_SPEC);
  assert.equal(packageDocument.dependencies?.[packageDocument.name], undefined);
  assert.equal(packageDocument.devDependencies?.esbuild, '0.25.6');
  assert.equal(packageDocument.devDependencies?.fflate, '0.8.2');

  const lock = await readJson(path.join(ROOT, 'package-lock.json'));
  const root = lock.packages?.[''];
  const signer = lock.packages?.[`node_modules/${SIGNER_PACKAGE}`];
  assert.equal(root?.dependencies?.[SIGNER_PACKAGE], SIGNER_SPEC);
  assert.equal(root?.dependencies?.[packageDocument.name], undefined);
  assert.equal(lock.packages?.[`node_modules/${packageDocument.name}`], undefined);
  assert.equal(root?.devDependencies?.fflate, '0.8.2');
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

function normalizeArchiveName(name) {
  const normalized = name.replaceAll('\\', '/');
  assert.equal(normalized.startsWith('/'), false, 'archive contains an absolute path');
  assert.equal(normalized.includes('../'), false, 'archive contains a parent path');
  assert.equal(normalized.endsWith('/'), false, 'archive contains a directory entry');
  return normalized;
}

function directoryView() {
  return Object.freeze({
    async read(filename) {
      return readFile(path.join(DIST, filename));
    },
  });
}

function archiveView(entries) {
  const normalized = new Map();
  for (const [name, bytes] of Object.entries(entries)) {
    const filename = normalizeArchiveName(name);
    assert.equal(normalized.has(filename), false, `duplicate archive entry: ${filename}`);
    normalized.set(filename, bytes);
  }
  return Object.freeze({
    names: Object.freeze([...normalized.keys()].sort()),
    async read(filename) {
      const bytes = normalized.get(filename);
      if (bytes === undefined) throw new Error(`archive is missing ${filename}`);
      return bytes;
    },
  });
}

async function jsonFromView(view, filename) {
  return JSON.parse(TEXT_DECODER.decode(await view.read(filename)));
}

async function auditArtifactView(view, {
  expectedSigningRule = null,
  requirePrivacyPolicy = false,
  requireSigningRule = false,
}) {
  const manifest = await jsonFromView(view, 'manifest.json');
  auditManifest(manifest);

  const metadata = await jsonFromView(view, BUILD_METADATA_FILE);
  assert.equal(metadata.schema, 'ofca-extension-build/v3');
  assert.equal(metadata.signer, `${SIGNER_PACKAGE}@${SIGNER_VERSION}`);
  assert.equal(metadata.signer_tarball, sha256(await readFile(SIGNER_TARBALL)));
  assert.equal(metadata.extension_id, EXPECTED_EXTENSION_ID);
  assert.equal(metadata.target, 'chrome116');
  assert.equal(metadata.determinism_verified, true);

  let artifactSigningRule = null;
  if (metadata.signing_rule !== null) {
    const bytes = await view.read(SIGNING_RULE_FILE);
    const document = validateSigningRuleDocument(JSON.parse(TEXT_DECODER.decode(bytes)));
    const canonicalBytes = TEXT_ENCODER.encode(stableJson(document));
    assert.equal(
      Buffer.compare(Buffer.from(bytes), Buffer.from(canonicalBytes)),
      0,
      'packaged signing rule is not canonical JSON',
    );
    artifactSigningRule = Object.freeze({
      document,
      bytes: canonicalBytes,
      digest: sha256(canonicalBytes),
    });
    assert.deepEqual(metadata.signing_rule, {
      schema: document.schema,
      source_revision: document.source_revision,
      sha256: artifactSigningRule.digest,
    });
  }
  if (requireSigningRule) assert.notEqual(artifactSigningRule, null);
  if (expectedSigningRule !== null) {
    assert.notEqual(artifactSigningRule, null);
    assert.equal(artifactSigningRule.digest, expectedSigningRule.digest);
    assert.deepEqual(artifactSigningRule.document, expectedSigningRule.document);
  }

  for (const filename of SCRIPT_FILES) {
    const bytes = await view.read(filename);
    assert.equal(metadata.outputs[filename], sha256(bytes));
    const source = TEXT_DECODER.decode(bytes);
    auditExecutableSource(filename, source);
    auditReadOnlyExecutable(filename, source);
    if (filename === 'background.js') {
      auditReadOnlyBackground(source);
      assert.match(source, /createChromeBrowserSigningProvider/);
      assert.match(source, /signer-state/);
      assert.match(source, /browser-signing-read\/v1/);
      assert.match(source, /active_account_partition_v5/);
      assert.match(source, /ofca_full_storage_bootstrap_v1/);
      assert.match(source, /ofca-idb-aesgcm\/v1/);
      assert.match(source, /\/api\/v1\/agent\/storage\/unseal/);
      assert.doesNotMatch(source, /pairing_auth_ticket/);
      assert.doesNotMatch(source, /browser_signing_state_v2:/);
      assert.doesNotMatch(source, /bridge-clean-dev-ticket|DEV_AUTH_TICKET|DEV_ACCOUNT_ID/);
      assert.doesNotMatch(source, /dev-creator-account/);
      auditSigningRuleBinding(source, artifactSigningRule, { required: requireSigningRule });
    }
  }

  const popup = TEXT_DECODER.decode(await view.read('popup.html'));
  assert.match(popup, /Observed in this browser/);
  assert.match(popup, /never message text/);
  assert.match(popup, /not affiliated with or endorsed by OnlyFans/);

  const extensionConfig = validateExtensionConfig(
    await jsonFromView(view, 'extension-config.json'),
    { requirePrivacyPolicy },
  );
  assert.equal(metadata.privacy_policy_configured, extensionConfig.privacy_policy_url !== '');

  for (const filename of UI_FILES) {
    assert.equal(metadata.outputs[filename], sha256(await view.read(filename)));
  }
  for (const filename of ICON_FILES) {
    const bytes = await view.read(filename);
    assert.ok(bytes.length > 0, `${filename} is empty`);
    assert.equal(metadata.outputs[filename], sha256(bytes));
  }
  const noticeBytes = await view.read(NOTICE_FILE);
  const notice = TEXT_DECODER.decode(noticeBytes);
  assert.match(notice, /local-authenticated-read-connector/);
  assert.match(notice, /MIT License/);
  assert.equal(metadata.outputs[NOTICE_FILE], sha256(noticeBytes));
  assert.equal(metadata.outputs['manifest.json'], sha256(await view.read('manifest.json')));

  const expectedOutputNames = [
    ...SCRIPT_FILES,
    ...UI_FILES,
    'manifest.json',
    ...ICON_FILES,
    NOTICE_FILE,
    ...(artifactSigningRule === null ? [] : [SIGNING_RULE_FILE]),
  ].sort();
  assert.deepEqual(Object.keys(metadata.outputs).sort(), expectedOutputNames);
  return metadata;
}

async function auditDirectory(
  expectedSigningRule = null,
  { requirePrivacyPolicy = false } = {},
) {
  await auditDependencyLock();
  return auditArtifactView(directoryView(), { expectedSigningRule, requirePrivacyPolicy });
}

export async function auditChromeArchive(filename, expectedSigningRule) {
  await auditDependencyLock();
  assert.notEqual(expectedSigningRule, null, 'archive audit requires an expected signing rule');
  const bytes = await readFile(filename);
  const view = archiveView(unzipSync(bytes));
  const metadata = await auditArtifactView(view, {
    expectedSigningRule,
    requirePrivacyPolicy: true,
    requireSigningRule: true,
  });
  const expectedNames = [...Object.keys(metadata.outputs), BUILD_METADATA_FILE].sort();
  assert.deepEqual(view.names, expectedNames, 'archive contains missing or unexpected files');
  return { metadata, digest: sha256(bytes) };
}

async function writeArtifact(compiled, signingRule, extensionConfig) {
  const sourceManifest = await readJson(path.join(ROOT, 'manifest.json'));
  auditManifest(sourceManifest);
  await rm(DIST, { force: true, recursive: true });
  await mkdir(path.join(DIST, 'icons'), { recursive: true });

  for (const [filename, bytes] of compiled) {
    await writeFile(path.join(DIST, filename), bytes);
  }
  for (const filename of [...MODE_SCRIPT_FILES, ...STATIC_UI_FILES]) {
    await copyFile(path.join(ROOT, filename), path.join(DIST, filename));
  }
  await writeFile(
    path.join(DIST, 'extension-config.json'),
    stableJson(extensionConfig),
    'utf8',
  );
  await writeFile(path.join(DIST, 'manifest.json'), stableJson(sourceManifest), 'utf8');
  for (const filename of ICON_FILES) {
    await copyFile(path.join(ROOT, filename), path.join(DIST, filename));
    const details = await stat(path.join(DIST, filename));
    assert.ok(details.size > 0, `${filename} is empty`);
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
  if (signingRule !== null) {
    await writeFile(path.join(DIST, SIGNING_RULE_FILE), signingRule.bytes);
  }

  const outputNames = [
    ...SCRIPT_FILES,
    ...UI_FILES,
    'manifest.json',
    ...ICON_FILES,
    NOTICE_FILE,
    ...(signingRule === null ? [] : [SIGNING_RULE_FILE]),
  ];
  const outputs = {};
  for (const filename of outputNames) {
    outputs[filename] = sha256(await readFile(path.join(DIST, filename)));
  }
  const metadata = {
    schema: 'ofca-extension-build/v3',
    extension_version: sourceManifest.version,
    extension_id: deriveExtensionId(sourceManifest.key),
    signer: `${SIGNER_PACKAGE}@${SIGNER_VERSION}`,
    signer_tarball: sha256(await readFile(SIGNER_TARBALL)),
    signing_rule: signingRule === null ? null : {
      schema: signingRule.document.schema,
      source_revision: signingRule.document.source_revision,
      sha256: signingRule.digest,
    },
    target: 'chrome116',
    determinism_verified: true,
    privacy_policy_configured: extensionConfig.privacy_policy_url !== '',
    outputs,
  };
  await writeFile(path.join(DIST, BUILD_METADATA_FILE), stableJson(metadata), 'utf8');
  return metadata;
}

async function writeChromeArchive(metadata) {
  const filename = path.join(
    DIST,
    `conversation-analytics-${metadata.extension_version}.zip`,
  );
  const archive = {};
  const names = [...Object.keys(metadata.outputs), BUILD_METADATA_FILE].sort();
  for (const name of names) {
    archive[name] = [await readFile(path.join(DIST, name)), { mtime: ZIP_TIMESTAMP }];
  }
  await writeFile(filename, zipSync(archive, { level: 9 }));
  return filename;
}

async function buildArtifact(signingRule, extensionConfig, { requirePrivacyPolicy }) {
  const first = await compileOnce(signingRule);
  const second = await compileOnce(signingRule);
  verifyIdenticalBuilds(first, second);
  const metadata = await writeArtifact(first, signingRule, extensionConfig);
  await auditDirectory(signingRule, { requirePrivacyPolicy });
  return metadata;
}

async function main() {
  if (process.argv.includes('--audit-package')) {
    const signingRule = await loadSigningRule({ required: true });
    await verifyLegalReleaseBindings({ required: true });
    const artifactArgument = argumentValue('--artifact');
    if (artifactArgument === null || artifactArgument.length === 0) {
      throw new Error('Chrome archive audit requires --artifact=<path>.');
    }
    const result = await auditChromeArchive(path.resolve(artifactArgument), signingRule);
    process.stdout.write(`Chrome archive audit passed (${result.digest}).\n`);
    return;
  }
  if (process.argv.includes('--audit')) {
    const signingRule = await loadSigningRule({ required: false });
    await verifyLegalReleaseBindings({ required: false });
    await auditDirectory(signingRule);
    process.stdout.write('Extension artifact audit passed.\n');
    return;
  }

  const packageRequested = process.argv.includes('--package');
  const signingRule = await loadSigningRule({ required: packageRequested });
  await verifyLegalReleaseBindings({ required: packageRequested });
  const extensionConfig = await loadExtensionConfig({
    requirePrivacyPolicy: packageRequested,
  });
  const metadata = await buildArtifact(signingRule, extensionConfig, {
    requirePrivacyPolicy: packageRequested,
  });
  if (packageRequested) {
    const filename = await writeChromeArchive(metadata);
    const result = await auditChromeArchive(filename, signingRule);
    process.stdout.write(`Chrome package created: ${filename} (${result.digest}).\n`);
    return;
  }
  process.stdout.write('Deterministic extension build and audit passed.\n');
}

const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) await main();
