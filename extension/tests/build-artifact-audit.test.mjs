import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import {
  auditExecutableSource,
  auditReadOnlyBackground,
  auditReadOnlyModuleGraph,
  auditSigningRuleBinding,
  deriveExtensionId,
  signerWrapperSource,
  validateExtensionConfig,
  validateSigningRuleDocument,
} from '../build.mjs';

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

test('signer wrapper binds one canonical packaged rule and has no caller fallback', () => {
  const source = signerWrapperSource(fixture);
  auditExecutableSource('signer-wrapper.js', source);
  auditSigningRuleBinding(source, fixture, { required: true });
  assert.match(source, /packagedRule: PACKAGED_SIGNING_RULE/);
  assert.doesNotMatch(source, /options\.packagedRule/);
});

test('signer wrapper without a packaged rule fails closed', () => {
  const source = signerWrapperSource(null);
  auditSigningRuleBinding(source, null, { required: false });
  assert.match(source, /unsupported_revision/);
  assert.match(source, /update the extension/i);
  assert.throws(() => auditSigningRuleBinding(source, null, { required: true }));
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
