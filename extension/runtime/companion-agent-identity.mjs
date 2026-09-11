import {
  PairingFailure,
  requirePairing,
  registrationMessage,
  proofMessage,
  enrollmentDigest,
  toHex,
  raw32,
} from "../transport/pairing-contract.mjs";

const ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);
async function sign(privateKey, message) {
  try {
    requirePairing(
      privateKey.type === "private" &&
        privateKey.extractable === false &&
        privateKey.algorithm.name === "ECDSA" &&
        privateKey.algorithm.namedCurve === "P-256",
    );
    const signature = new Uint8Array(
      await crypto.subtle.sign(
        { name: "ECDSA", hash: "SHA-256" },
        privateKey,
        message,
      ),
    );
    requirePairing(signature.length === 64);
    const s = BigInt("0x" + toHex(signature.slice(32)));
    if (s > ORDER / 2n)
      signature.set(raw32((ORDER - s).toString(16).padStart(64, "0")), 32);
    return signature;
  } catch {
    throw new PairingFailure("pairing_proof_refused");
  }
}
// The registration assignment must come from the authenticated human ceremony.
// This helper neither registers the identity nor transports customer credentials.
export async function signAgentRegistration(
  identity,
  { challenge, organizationId, agentId, keyId, thumbprint },
) {
  requirePairing(thumbprint === identity.thumbprint);
  return sign(
    identity.privateKey,
    registrationMessage(challenge, organizationId, agentId, keyId, thumbprint),
  );
}
export async function signAgentPairing(identity, expected, challenge) {
  const context = structuredClone(expected);
  requirePairing(challenge instanceof Uint8Array && challenge.length === 32);
  const nonce = challenge.slice();
  const privateKey = identity.privateKey;
  requirePairing(context.agent_identity_key_jkt === identity.thumbprint);
  return sign(
    privateKey,
    proofMessage(
      "agent",
      nonce,
      await enrollmentDigest(context),
      context.agent_identity_key_id,
    ),
  );
}
export function snowKeypairGenerator(generateStaticKeypair) {
  return () => {
    const bytes = generateStaticKeypair();
    requirePairing(bytes instanceof Uint8Array && bytes.length === 64);
    try {
      return { privateKey: bytes.slice(0, 32), publicKey: bytes.slice(32) };
    } finally {
      bytes.fill(0);
    }
  };
}
