import Ajv from 'ajv';

import schema from '../../../shared/onboarding/schema.json';

const check = new Ajv({ allErrors: false, strictNumbers: true }).compile(schema);
const encode = new TextEncoder();

/** Shape validation only: the transport authenticates the owner separately. */
export function validateOnboardingMessage(value: unknown): boolean {
  try {
    const serialized = JSON.stringify(value);
    return typeof serialized === 'string' && encode.encode(serialized).byteLength <= 4096
      && check(value) === true;
  } catch { return false; }
}
