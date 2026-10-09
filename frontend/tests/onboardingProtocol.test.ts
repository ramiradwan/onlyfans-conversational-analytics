import { describe, expect, it } from 'vitest';

import vectors from '../../shared/onboarding/vectors.json';
import { validateOnboardingMessage } from '../src/protocol/onboarding';

describe('production local onboarding validator', () => {
  it.each(vectors.cases)('$id', ({ value, valid }) => {
    expect(validateOnboardingMessage(value)).toBe(valid);
  });
  it('rejects nonfinite revisions instead of accepting JSON coercion', () => {
    const message = vectors.cases.find((value) => value.id === 'extension-snapshot')!.value;
    expect(validateOnboardingMessage({ ...message, revision: Infinity })).toBe(false);
  });
});
