import {
  PairingFailure,
  b64u,
  normalizeSignature,
  proofMessage,
  lp,
  key32,
  requirePairing,
} from "../transport/pairing-contract.mjs";

async function sign(privateKey, message) {
  try {
    requirePairing(
      privateKey.type === "private" &&
        privateKey.extractable === false &&
        privateKey.algorithm.name === "ECDSA" &&
        privateKey.algorithm.namedCurve === "P-256",
    );
    return normalizeSignature(
      new Uint8Array(
        await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, message),
      ),
    );
  } catch {
    throw new PairingFailure("pairing_proof_refused");
  }
}

/** Agent pairing proof over a verified pairing digest, as unpadded base64url. */
export async function signPairingProof(identity, pairingDigest) {
  const message = proofMessage("agent", pairingDigest.slice());
  return b64u(await sign(identity.privateKey, message));
}

export async function signAgentSessionProof(identity, challenge, pin, agentInstallationId) {
  requirePairing(typeof challenge?.challenge === 'string' && /^[A-Za-z0-9_-]{43}$/u.test(challenge.challenge));
  key32(challenge.challenge);
  requirePairing(typeof challenge.session_id === 'string' && /^[A-Za-z0-9._~-]{1,128}$/u.test(challenge.session_id));
  const message = lp('OFCA-AGENT-REQUEST-V1', [
    challenge.session_id, challenge.challenge, 'POST', '/agent/session-ticket',
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    'agent-websocket', agentInstallationId, pin.creator_account_id,
    pin.pairing_id, pin.installation_id,
  ]);
  return b64u(await sign(identity.privateKey, message));
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
