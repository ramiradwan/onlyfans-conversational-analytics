export const PROFILE = 'Noise_KK_25519_ChaChaPoly_SHA256';
export const AGENT_PRIVATE_HEX = '101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f';
export const AGENT_PUBLIC_HEX = 'd89e3bad79437dbed9f843418304f460ff05c7fe81fe4a9577a804cb9367ff66';
export const BRAIN_PRIVATE_HEX = '404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f';
export const BRAIN_PUBLIC_HEX = '79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a';
export const APP_PAYLOAD = new TextEncoder().encode('snow-wasm-agent-application');
export const APP_REPLY = new TextEncoder().encode('python-brain-application');

// expected.pairing_digest of the vendored companion-pairing contract; this
// module also loads in the browser, so binding.test.mjs checks the literal.
export const PAIRING_DIGEST_HEX = 'da282a55e299bcd3893680db428f699bff8e340a8f87e5cb20e9808b1dc1c62f';
