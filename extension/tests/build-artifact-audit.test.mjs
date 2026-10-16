import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  auditExecutableSource,
  auditReadOnlyBackground,
  auditReadOnlyModuleGraph,
  auditSigningRuleBinding,
  deriveExtensionId,
  signerWrapperSource,
  validateExtensionConfig,
  validateOnboardingReleaseConfig,
  validateSigningRuleDocument,
} from '../build.mjs';

test('Agent packaging consumes the current desktop release document and closed provider choices', () => {
  const source = JSON.parse(readFileSync(new URL('../../app/core/customer-release.json', import.meta.url), 'utf8'));
  assert.equal(validateOnboardingReleaseConfig(source), 'https://onboarding-dev.dipsy.fi');
  const configured = { ...source, setup_transfer_receiver_url: 'https://setup.example.test/public/onboarding/setup',
    hosted_api_origin: 'https://setup.example.test', authentication_providers: ['google', 'microsoft-personal', 'default'] };
  assert.equal(validateOnboardingReleaseConfig(configured, { required: true }), 'https://setup.example.test');
  for (const authentication_providers of [undefined, null, [], ['google'], ['default', 'default'],
    ['https://identity.example.test', 'default'], 'default', [true, 'default']]) {
    assert.throws(() => validateOnboardingReleaseConfig({ ...configured, authentication_providers }));
  }
  const { authentication_providers: _providers, ...missing } = configured;
  assert.throws(() => validateOnboardingReleaseConfig(missing));
  assert.equal(validateOnboardingReleaseConfig({ ...configured, setup_transfer_receiver_url: '' }, { required: true }), configured.hosted_api_origin);
  assert.equal(validateOnboardingReleaseConfig({ ...configured, setup_transfer_receiver_url: '', hosted_api_origin: '' }), null);
  for (const setup_transfer_receiver_url of ['https://setup.example.test/',
    'https://setup.example.test/onboarding/browser-ceremonies/{reference}',
    'https://other.example.test/public/onboarding/setup',
    'https://setup.example.test/public/onboarding/setup?reference=secret']) {
    assert.throws(() => validateOnboardingReleaseConfig({ ...configured, setup_transfer_receiver_url }));
  }
  assert.throws(() => validateOnboardingReleaseConfig({ ...configured, schema: 'ofca-customer-release/v1' }));
  assert.throws(() => validateOnboardingReleaseConfig({ ...configured, hosted_api_origin: '' }));
  assert.throws(() => validateOnboardingReleaseConfig({ ...configured, setup_transfer_receiver_url: '', hosted_api_origin: '' }, { required: true }));
});

const fixtureRule = validateSigningRuleDocument({
  schema: 'local-packaged-signing-rule/v1',
  source_revision: 'test-fixture-revision',
  static_param: 'test-fixture-static-param',
  format: {
    prefix: 'test-prefix',
    suffix: 'test-suffix',
    checksum_indexes: [0, 7, 13],
    checksum_constant: 17,
  },
});
const fixtureBytes = new TextEncoder().encode(`${JSON.stringify(fixtureRule, null, 2)}\n`);
const fixture = Object.freeze({
  document: fixtureRule,
  bytes: fixtureBytes,
  digest: `sha256:${createHash('sha256').update(fixtureBytes).digest('hex')}`,
});

test('extension ID is derived from the manifest public key', () => {
  const key = 'MAowBQYDK2VwAyEAoPyN1ATSl3x0Rj/lBvoIwuwifPXjNpKF1esnhTCc/pI=';
  assert.match(deriveExtensionId(key), /^[a-p]{32}$/);
  assert.notEqual(deriveExtensionId(key), deriveExtensionId(`${key.slice(0, -2)}AA`));
});

test('dynamic wrapper rejects raw rules and requires the nominal verified source', () => {
  assert.throws(() => signerWrapperSource(fixture));
  const source = signerWrapperSource(null);
  auditExecutableSource('signer-wrapper.js', source);
  assert.match(source, /isVerifiedSigningParameterSource/);
  assert.doesNotMatch(source, /PACKAGED_SIGNING_RULE_B64/);
});

test('packaged dynamic signer fixes observe-only and refuses unsigned caller modes', async () => {
  const asModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
  const dependency = asModule('export const isVerifiedSigningParameterSource = value => value?.nominal === true; export async function createChromeBrowserSigningProvider(options) { return options; }');
  const wrapper = await import(asModule(signerWrapperSource(null)
    .replaceAll("'packaged-signer-implementation'", JSON.stringify(dependency))));
  await assert.rejects(wrapper.createChromeBrowserSigningProvider({ packagedRule: fixtureRule }));
  await assert.rejects(wrapper.createChromeBrowserSigningProvider({ parameterSource: {} }));
  const source = { nominal: true };
  const options = await wrapper.createChromeBrowserSigningProvider({ captureMode: 'safe-reload', parameterSource: source });
  assert.equal(options.captureMode, 'observe-only');
  assert.equal(options.parameterSource, source);
});

test('executable audit rejects runtime discovery and executable-code factories', () => {
  const forbidden = [
    'runtime-discovery()',
    'webpackChunk.push([])',
    '__webpack_require__(7)',
    'executeModuleFactory(candidate)',
    'eval(candidate)',
    'new Function(candidate)',
    'Function(candidate)',
    "importScripts('https://example.test/code.js')",
    "import('https://example.test/code.js')",
    'fetchRemoteSigningRule()',
    'signingRuleFallback()',
  ];
  for (const source of forbidden) {
    assert.throws(
      () => auditExecutableSource('background.js', source),
      source,
    );
  }
});

test('packaged signing rule grammar rejects extra and missing fields', () => {
  assert.throws(() => validateSigningRuleDocument({
    ...fixtureRule,
    headers: { cookie: 'not-allowed' },
  }));
  assert.throws(() => validateSigningRuleDocument({
    ...fixtureRule,
    source_revision: '',
  }));
});

test('read-only module graph excludes full command and protocol modules', () => {
  const allowed = [
    'background-read-only.js',
    'protocol/read-only.mjs',
    'transport/agent-runtime-core.mjs',
    'transport/read-only-agent-runtime.mjs',
  ];
  assert.deepEqual(auditReadOnlyModuleGraph(allowed), [...allowed].sort());
  for (const forbidden of [
    'transport/chrome-adapter-core.mjs',
    'transport/read-only-chrome-adapter.mjs',
    'transport/read-only-config-http-adapter.mjs',
    'transport/secure-local-fetch.mjs',
    'background.js',
    'protocol/index.mjs',
    'protocol/validation.mjs',
    'transport/agent-command-service.mjs',
    'transport/agent-runtime.mjs',
    'transport/agent-websocket.mjs',
    'tests/helper.mjs',
  ]) {
    assert.throws(() => auditReadOnlyModuleGraph([...allowed, forbidden]), forbidden);
  }
});

test('read-only background binds exactly four capabilities and rejects mutation channels', () => {
  const source = `
    const READ_ONLY_CAPABILITIES = Object.freeze([
      'capture.chats',
      'capture.messages',
      'capture.presence',
      'history.sync'
    ]);
    const runtime = { capabilities: READ_ONLY_CAPABILITIES };
    const hello = { capabilities: this.capabilities };
  `;
  auditReadOnlyBackground(source);
  for (const forbidden of [
    "'command.execute'",
    "'command.result'",
    "'command.message.send'",
    "'message.send'",
    '_OF_BACKEND_',
    'send_ws_message',
    'send_fetch_command',
  ]) {
    assert.throws(() => auditReadOnlyBackground(`${source}\n${forbidden}`), forbidden);
  }
});

test('Chrome package configuration requires an HTTPS privacy policy', () => {
  const base = {
    schema: 'ofca-extension-config/v1',
    privacy_policy_url: '',
    dashboard_url: 'http://bridge.localhost:17871/',
    history_settings_url: 'http://bridge.localhost:17871/settings',
  };
  assert.deepEqual(validateExtensionConfig(base), base);
  assert.throws(() => validateExtensionConfig(base, { requirePrivacyPolicy: true }));
  assert.throws(() => validateExtensionConfig({
    ...base,
    privacy_policy_url: 'http://example.test/privacy',
  }, { requirePrivacyPolicy: true }));
  assert.equal(validateExtensionConfig({
    ...base,
    privacy_policy_url: 'https://example.test/privacy',
  }, { requirePrivacyPolicy: true }).privacy_policy_url, 'https://example.test/privacy');
});
