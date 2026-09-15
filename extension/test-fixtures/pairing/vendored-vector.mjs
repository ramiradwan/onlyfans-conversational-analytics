// Loads the companion-pairing contract vendored under contracts/ and returns
// only bytes that match the contract manifest and the independent consumer
// pin. Nothing here generates fixture material: the published labels derive it.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

const CONTRACTS = new URL("../../../contracts/", import.meta.url);
const EXPORT = "companion-pairing-v1";
const PROFILE_EXPORT = "companion-pairing-profile";
const LABEL_PREFIX = "OFCA TEST VECTORS ONLY - NEVER PRODUCTION - ";
const P256_ORDER = BigInt(
  "0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551",
);

export class VendoredContractError extends Error {}

const sha256 = (bytes) => createHash("sha256").update(bytes).digest();
const b64u = (bytes) => Buffer.from(bytes).toString("base64url");

function check(condition, message) {
  if (!condition) throw new VendoredContractError(message);
}

async function readBytes(relative) {
  return readFile(new URL(relative, CONTRACTS));
}

function aggregateDigest(entries) {
  return sha256(
    Buffer.concat(
      entries.map((entry) =>
        Buffer.concat([
          Buffer.from(entry.path, "utf8"),
          Buffer.from([0]),
          Buffer.from(entry.sha256, "ascii"),
          Buffer.from("\n", "ascii"),
        ]),
      ),
    ),
  ).toString("hex");
}

async function pinnedBytes() {
  const manifestBytes = await readBytes("manifest.json");
  const pin = JSON.parse(await readBytes("consumer-pin.json"));
  const manifest = JSON.parse(manifestBytes);
  check(
    pin.contract_manifest_sha256 === sha256(manifestBytes).toString("hex"),
    "consumer pin does not match contract manifest",
  );
  check(
    pin.aggregate_bundle_sha256 === manifest.content_digest &&
      manifest.content_digest === aggregateDigest(manifest.files),
    "contract manifest does not match its aggregate digest",
  );
  check(
    pin.export_set.includes(EXPORT) && pin.export_set.includes(PROFILE_EXPORT),
    "the companion-pairing exports are not pinned",
  );
  const entries = new Map(manifest.files.map((entry) => [entry.path, entry]));
  return async function pinned(relative) {
    const entry = entries.get(relative);
    check(entry !== undefined, `contract manifest does not list ${relative}`);
    const bytes = await readBytes(relative);
    check(
      bytes.length === entry.size && sha256(bytes).toString("hex") === entry.sha256,
      `vendored bytes differ from the pinned digest: ${relative}`,
    );
    return bytes;
  };
}

async function loadFamily() {
  const pinned = await pinnedBytes();
  const familyManifest = JSON.parse(await pinned(`${EXPORT}/manifest.json`));
  check(
    familyManifest.profile === "urn:bridge-clean:companion-pairing:v1",
    "the vendored family names another profile",
  );
  const files = new Map();
  for (const entry of familyManifest.files) {
    const bytes = await pinned(`${EXPORT}/${entry.path}`);
    check(
      bytes.length === entry.size && sha256(bytes).toString("hex") === entry.sha256,
      `the family manifest disagrees with the contract manifest: ${entry.path}`,
    );
    files.set(entry.path, JSON.parse(bytes));
  }
  const profileRecord = JSON.parse(await pinned(`${PROFILE_EXPORT}/profile.json`));
  check(
    profileRecord.profile === familyManifest.profile,
    "the vendored profile record and vectors name different profiles",
  );
  return { files, profileRecord };
}

const { files, profileRecord } = await loadFamily();

export const profile = profileRecord;
export const vector = files.get("vector.json");
export const trustSet = files.get("trust-set.json");
export const offerCases = files.get("offer-cases.json");
export const requestCases = files.get("request-cases.json");
export const confirmCases = files.get("confirm-cases.json");
export const authorizationCases = files.get("authorization-cases.json");

/** Derive the published test-only material a fixture label stands for. */
export function fixtureMaterial(label) {
  return new Uint8Array(sha256(Buffer.from(`${LABEL_PREFIX}${label}`, "ascii")));
}

/** Rebuild a fixture P-256 private JWK from its label and published point. */
export function fixturePrivateJwk(label, publicJwk) {
  const material = BigInt(`0x${Buffer.from(fixtureMaterial(label)).toString("hex")}`);
  const scalar = (material % (P256_ORDER - 1n)) + 1n;
  return {
    ...publicJwk,
    d: b64u(Buffer.from(scalar.toString(16).padStart(64, "0"), "hex")),
  };
}
