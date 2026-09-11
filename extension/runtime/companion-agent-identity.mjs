import {
  PairingFailure,
  b64u,
  normalizeSignature,
  proofMessage,
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
