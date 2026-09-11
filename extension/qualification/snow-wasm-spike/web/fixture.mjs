export const PROFILE = 'Noise_KK_25519_ChaChaPoly_SHA256';
export const SPIKE_PROLOGUE_PREFIX = 'ofca-session-spike/v1;agent-to-brain;no-early-data';
export const AGENT_PRIVATE_HEX = '101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f';
export const AGENT_PUBLIC_HEX = 'd89e3bad79437dbed9f843418304f460ff05c7fe81fe4a9577a804cb9367ff66';
export const BRAIN_PRIVATE_HEX = '404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f';
export const BRAIN_PUBLIC_HEX = '79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a';
export const APP_PAYLOAD = new TextEncoder().encode('snow-wasm-agent-application');
export const APP_REPLY = new TextEncoder().encode('python-brain-application');

function b64u(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/u, '');
}

export const FIXED_CLAIMS = Object.freeze({
  iss: 'https://pairing.example', aud: 'urn:ofca:companion-pairing:v1',
  iat: 1800000000, exp: 1800000300, jti: '01'.repeat(32),
  suite: PROFILE, organization_id: 'org-1', installation_id: 'installation-1',
  installation_key_id: 'ik-1', installation_key_jkt: b64u(Uint8Array.from({length: 32}, (_, i) => i)),
  agent_id: 'agent-1', agent_identity_key_id: 'ak-1',
  agent_identity_key_jkt: b64u(Uint8Array.from({length: 32}, (_, i) => i + 32)),
  account_id: 'account-1', pairing_id: '02'.repeat(32),
  agent_key: b64u(Uint8Array.from({length: 32}, (_, i) => i + 64)),
  brain_key: b64u(Uint8Array.from({length: 32}, (_, i) => i + 96)),
  agent_nonce: '03'.repeat(32), brain_nonce: '04'.repeat(32), generation: 7,
  grant_digest: '05'.repeat(32), approval_id: 'approval-7', approval_revision: 3,
  offline_not_after: 1800086400,
});
export const EXPECTED_BINDING_HEX = 'a3c1caeb43855787de35bf7081d5b70912bdd2027d27be1bdf4843eb86befe61';
