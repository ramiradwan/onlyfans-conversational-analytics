export const DELETE_INTENT_KEY = 'ofca_delete_intent_v1';
export const DELETE_INTENT_SCHEMA = 'ofca-delete-intent/v1';

export function deletionIntent(now = new Date()) {
  return Object.freeze({
    schema: DELETE_INTENT_SCHEMA,
    requested_at: now.toISOString(),
  });
}

export function isDeletionIntent(value) {
  return value?.schema === DELETE_INTENT_SCHEMA
    && typeof value.requested_at === 'string'
    && Number.isFinite(Date.parse(value.requested_at));
}
