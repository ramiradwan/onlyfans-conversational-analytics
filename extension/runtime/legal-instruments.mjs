export const LEGAL_INSTRUMENT_BINDINGS_SCHEMA = 'ofca-legal-instrument-bindings/v1';
export const LEGAL_ACTIVATION_SCHEMA_VERSION = '2.0';
export const LEGAL_INSTRUMENT_NAMES = Object.freeze([
  'terms_of_service',
  'privacy_policy',
  'extension_privacy_notice',
  'risk_disclosure',
]);

const SEMVER = /^[0-9]+\.[0-9]+\.[0-9]+$/;
const SHA256 = /^[a-f0-9]{64}$/;
const GIT_SHA1 = /^[a-f0-9]{40}$/;
const PUBLIC_ROUTE = /^\/[a-z0-9_/-]+$/;
const LOCALE = /^[a-z]{2}(?:-[A-Z]{2})?$/;

function exactKeys(value, expected, label) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new TypeError(`${label} contains unexpected or missing fields`);
  }
}

function validateInstrument(value, label) {
  exactKeys(value, ['version', 'rendered_sha256', 'public_url', 'locale'], label);
  if (!SEMVER.test(value.version)) throw new TypeError(`${label}.version is invalid`);
  if (!SHA256.test(value.rendered_sha256)) {
    throw new TypeError(`${label}.rendered_sha256 is invalid`);
  }
  if (!PUBLIC_ROUTE.test(value.public_url)) throw new TypeError(`${label}.public_url is invalid`);
  if (!LOCALE.test(value.locale)) throw new TypeError(`${label}.locale is invalid`);
  return Object.freeze({ ...value });
}

export function validateLegalInstrumentBindings(value, { allowInvalidTestOrigin = false } = {}) {
  exactKeys(
    value,
    [
      'schema',
      'legal_repository_revision',
      'activation_schema',
      'public_origin',
      'instruments',
    ],
    'legal instrument bindings',
  );
  if (value.schema !== LEGAL_INSTRUMENT_BINDINGS_SCHEMA) {
    throw new TypeError('legal instrument bindings schema is unsupported');
  }
  if (!GIT_SHA1.test(value.legal_repository_revision)) {
    throw new TypeError('legal_repository_revision must be a 40-character Git commit SHA');
  }
  exactKeys(value.activation_schema, ['version', 'blob_sha'], 'activation_schema');
  if (value.activation_schema.version !== LEGAL_ACTIVATION_SCHEMA_VERSION) {
    throw new TypeError('activation schema version must remain 2.0');
  }
  if (!GIT_SHA1.test(value.activation_schema.blob_sha)) {
    throw new TypeError('activation_schema.blob_sha must be a Git blob SHA');
  }

  const publicOrigin = new URL(value.public_origin);
  if (
    publicOrigin.protocol !== 'https:'
    || publicOrigin.username !== ''
    || publicOrigin.password !== ''
    || publicOrigin.pathname !== '/'
    || publicOrigin.search !== ''
    || publicOrigin.hash !== ''
  ) {
    throw new TypeError('legal public_origin must be an HTTPS origin with no path or credentials');
  }
  if (!allowInvalidTestOrigin && publicOrigin.hostname.endsWith('.invalid')) {
    throw new TypeError('legal public_origin must resolve to a production-capable hostname');
  }

  exactKeys(value.instruments, LEGAL_INSTRUMENT_NAMES, 'legal instruments');
  const instruments = Object.fromEntries(LEGAL_INSTRUMENT_NAMES.map((name) => (
    [name, validateInstrument(value.instruments[name], `instruments.${name}`)]
  )));
  const locales = new Set(Object.values(instruments).map((instrument) => instrument.locale));
  if (locales.size !== 1) {
    throw new TypeError('all activation instruments must use the same locale');
  }

  return Object.freeze({
    schema: value.schema,
    legal_repository_revision: value.legal_repository_revision,
    activation_schema: Object.freeze({ ...value.activation_schema }),
    public_origin: publicOrigin.origin,
    instruments: Object.freeze(instruments),
  });
}

export function presentedInstruments(bindings) {
  const validated = validateLegalInstrumentBindings(bindings);
  return structuredClone(validated.instruments);
}

export function absoluteInstrumentUrl(bindings, instrumentName) {
  if (!LEGAL_INSTRUMENT_NAMES.includes(instrumentName)) {
    throw new TypeError(`Unknown legal instrument ${instrumentName}`);
  }
  const validated = validateLegalInstrumentBindings(bindings);
  return new URL(validated.instruments[instrumentName].public_url, validated.public_origin).href;
}
