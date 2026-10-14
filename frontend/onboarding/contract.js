import { validateDocument, validateOperation } from './generated-contract.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

export function snapshot(text, generation) {
  const value = parseOnboardingJson(text);
  if (!validateDocument(value) || value.kind !== 'snapshot' || value.generation !== generation) throw new Error('invalid_snapshot');
  return value;
}

export function command(value, action, operationId, options = {}) {
  const result = { profile: 'desktop-onboarding.v1', kind: 'command', journey_id: value.journey_id,
    operation_id: operationId, action, expected: { generation: value.generation, revision: value.revision, scope_digest: value.scope_digest } };
  if (Object.keys(options).some(name => name !== 'provider')) throw new Error('action_unavailable');
  if (options.provider !== undefined) result.provider = options.provider;
  if (!validateDocument(result) || !value.allowed_actions.includes(action)) throw new Error('action_unavailable');
  return result;
}

export function operation(value) {
  if (!validateOperation(value)) throw new Error('invalid_receipt');
  return value;
}
