import assert from 'node:assert/strict';

export const RELEASE_SCENARIOS = Object.freeze([
  'account_document_switch_and_delayed_capture',
  'disclosure_change_and_reacceptance',
  'interrupted_deletion_and_same_worker_reactivation',
  'suspension_during_startup_and_write',
  'lost_ack_offline_retry_and_queue_limits',
  'native_permission_removal_and_restoration',
  'authenticated_noise_pairing_and_failure_deadlines',
  'hostile_listener_and_ambient_credential_isolation',
  'paired_worker_reconstruction_and_revocation',
  'fresh_install_companion_unavailable',
]);

// Fixture browser automation cannot certify production provisioning or operate
// native Chrome permission prompts. Require recorded tests of the same ZIP.
export function validateAcceptanceEvidence(evidence, { artifactDigest, sourceRevision, currentMajor }) {
  assert.ok(Number.isSafeInteger(currentMajor) && currentMajor > 132, 'current browser must be newer than the minimum');
  assert.equal(evidence?.schema, 'ofca-extension-release-acceptance/v2');
  assert.equal(evidence.artifact_sha256, artifactDigest, 'acceptance evidence is for a different ZIP');
  assert.equal(evidence.source_revision, sourceRevision, 'acceptance evidence is for a different revision');
  assert.equal(evidence.companion_transport, 'ws://127.0.0.1:17871/ws/agent');
  assert.equal(evidence.cryptographic_session, 'Noise_KK_25519_ChaChaPoly_SHA256');
  assert.equal(evidence.pairing, 'verified_grants_and_comparison');
  assert.equal(evidence.ambient_credentials, 'absent');
  assert.equal(evidence.authentication, 'verified');
  assert.equal(evidence.permission_prompt, 'native');
  assert.equal(evidence.bypasses_used, false, 'release qualification cannot use bypasses');
  assert.ok(typeof evidence.companion_version === 'string' && evidence.companion_version.length > 0);
  assert.ok(typeof evidence.tester === 'string' && evidence.tester.trim().length > 0);
  assert.ok(Number.isFinite(Date.parse(evidence.performed_at)));
  assert.equal(evidence.browsers?.length, 2, 'acceptance requires the exact two-browser matrix');
  for (const major of [132, currentMajor]) {
    const recordsForBrowser = evidence.browsers.filter((entry) => entry.major === major);
    assert.equal(recordsForBrowser.length, 1, `missing or duplicate native Chrome ${major} acceptance evidence`);
    const browser = recordsForBrowser[0];
    assert.equal(browser.installation, 'supported');
    for (const scenario of RELEASE_SCENARIOS) {
      const records = browser.scenarios?.filter((entry) => entry.id === scenario) ?? [];
      assert.equal(records.length, 1, `missing or duplicate ${scenario} evidence for Chrome ${major}`);
      assert.equal(records[0].result, 'passed', `${scenario} has not passed`);
      assert.ok(typeof records[0].evidence === 'string' && records[0].evidence.trim().length > 0,
        `${scenario} needs an evidence reference`);
    }
  }
  return structuredClone(evidence);
}
