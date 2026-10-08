// @vitest-environment node
import Ajv from 'ajv';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const bundle = resolve('..', 'shared', 'onboarding');
const schema = JSON.parse(readFileSync(resolve(bundle, 'schema.json'), 'utf8'));
const cases: { id: string; value: unknown; valid: boolean }[] = JSON.parse(readFileSync(resolve(bundle, 'vectors.json'), 'utf8')).cases;
const validate = new Ajv({ allErrors: true }).compile(schema);

describe('local onboarding contract independent browser validator', () => {
  for (const vector of cases) {
    it(vector.id, () => expect(validate(vector.value)).toBe(vector.valid));
  }
});
