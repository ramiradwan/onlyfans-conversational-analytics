export const SIGNING_PARAMETER_METHOD = 'agent.signing-rule.resolve';
export const SIGNING_PARAMETER_CAPABILITY = 'signing.parameters.v1';
export const PARAMETER_FAILURES = new Set(['parameters_pending', 'parameters_unavailable',
  'parameter_publication_rejected', 'parameter_engine_unsupported']);
export function validateParameterRequest(request) {
  if (!request || Object.keys(request).sort().join(',') !== 'catalog_sequence,engine,keyset_sequence,object_sha256,parameter_schema,schema,source_revision'
    || request.schema !== 'signing-parameter-request/v1' || request.engine !== 'onlyfans-sha1-checksum/v1'
    || request.parameter_schema !== 'local-packaged-signing-rule/v1' || typeof request.source_revision !== 'string'
    || !/^[\x21-\x7e]{1,128}$/.test(request.source_revision)
    || ![request.catalog_sequence, request.keyset_sequence].every(value => Number.isSafeInteger(value) && value >= 0)
    || (request.object_sha256 !== null && (typeof request.object_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(request.object_sha256)))) {
    throw new TypeError('Invalid signing parameter request');
  }
  return request;
}
export function parameterDependency(error) {
  if (!PARAMETER_FAILURES.has(error?.code)) return null;
  const revision = error.neededRevision;
  if (typeof revision !== 'string' || !/^[\x21-\x7e]{1,128}$/.test(revision)) return null;
  return { reason: error.code, needed_revision: revision,
    retry_after_ms: Math.min(300_000, Math.max(1_000, error.parameter_retry_after_ms ?? 30_000)) };
}
export function validateParameterWait(wait) {
  if (!wait || Object.keys(wait).sort().join(',') !== 'needed_revision,next_attempt_at,reason'
    || !PARAMETER_FAILURES.has(wait.reason) || typeof wait.needed_revision !== 'string'
    || !/^[\x21-\x7e]{1,128}$/.test(wait.needed_revision)
    || !Number.isSafeInteger(wait.next_attempt_at) || wait.next_attempt_at < 0) {
    throw new Error('Invalid signing dependency wait');
  }
  return wait;
}
