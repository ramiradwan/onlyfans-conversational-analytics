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
  if (Object.keys(options).some(name => !['provider', 'replace_ceremony', 'recover_authority'].includes(name))) throw new Error('action_unavailable');
  if (options.provider !== undefined) result.provider = options.provider;
  if (options.replace_ceremony !== undefined) result.replace_ceremony = options.replace_ceremony;
  if (options.recover_authority !== undefined) result.recover_authority = options.recover_authority;
  if (value.authority_recovery !== undefined) {
    const recoveryParent = value.authority_recovery === 'available' && action === 'account.connect';
    const recoveryChild = value.authority_recovery === 'sign-in-required'
      && (action === 'auth.select' && value.authentication?.actions.select && result.provider === 'default'
        || action === 'auth.cancel' && value.authentication?.actions.cancel);
    if (result.recover_authority !== true || !(recoveryParent || recoveryChild)) throw new Error('action_unavailable');
  } else if (result.recover_authority !== undefined) throw new Error('action_unavailable');
  if (result.replace_ceremony && value.authentication?.state !== 'replacement-required') throw new Error('action_unavailable');
  if (!validateDocument(result) || !value.allowed_actions.includes(action)) throw new Error('action_unavailable');
  return result;
}

export function operation(value) {
  if (!validateOperation(value)) throw new Error('invalid_receipt');
  return value;
}
