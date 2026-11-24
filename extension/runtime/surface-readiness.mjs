import { exact, UUID } from '../../shared/browser-tasks/wire.mjs';

export const READINESS_REQUEST = 'readiness.v1';
export const READINESS_RESPONSE = 'analysis_readiness.v1';

export function isReadinessRequest(value) {
  return exact(value, ['type', 'request_id']) && value.type === READINESS_REQUEST
    && typeof value.request_id === 'string' && UUID.test(value.request_id);
}

export function isReadiness(value) {
  return ['required', 'active', 'unavailable'].includes(value.commercial_authority)
    && ['blocked', 'admitted'].includes(value.analysis_admission)
    && (value.analysis_admission !== 'admitted' || value.commercial_authority === 'active');
}

export function isReadinessResponse(value) {
  return exact(value, ['type', 'request_id', 'commercial_authority', 'analysis_admission'])
    && value.type === READINESS_RESPONSE
    && typeof value.request_id === 'string' && UUID.test(value.request_id) && isReadiness(value);
}
