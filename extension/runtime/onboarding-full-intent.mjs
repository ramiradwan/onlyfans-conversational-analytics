// Presentation intent only: never consent, creator authority or pairing.
export const FULL_REVIEW_INTENT_KEY = 'onboarding_full_intent_v2';
const LAST_REQUEST_KEY = 'onboarding-full-review-request';
const REVIEW_STATE_KEY = 'onboarding-full-review-state-v2';
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
function validScope(record) {
  return record && uuid.test(record.journey_id) && uuid.test(record.draft_scope?.scope_id)
    && /^[0-9a-f]{64}$/u.test(record.draft_scope?.disclosure_bundle_id);
}
function sameScope(left, right) {
  return validScope(left) && validScope(right) && left.journey_id === right.journey_id
    && left.draft_scope.scope_id === right.draft_scope.scope_id
    && left.draft_scope.disclosure_bundle_id === right.draft_scope.disclosure_bundle_id;
}
// Tab presentation survives reload only after the worker admits this exact
// journey, creator draft and disclosure. Legacy unscoped state is discarded.
export function createFullReviewPersistence(storage = globalThis.sessionStorage) {
  try { storage?.removeItem('full-review'); } catch {}
  return {
    read(record) {
      try {
        const saved = JSON.parse(storage?.getItem(REVIEW_STATE_KEY) ?? 'null');
        if (sameScope(saved, record) && typeof saved.requested === 'boolean') return saved.requested;
        storage?.removeItem(REVIEW_STATE_KEY);
      } catch {}
      return false;
    },
    write(record, requested) {
      if (!validScope(record)) return;
      try { storage?.setItem(REVIEW_STATE_KEY, JSON.stringify({ journey_id: record.journey_id,
        draft_scope: { scope_id: record.draft_scope.scope_id, disclosure_bundle_id: record.draft_scope.disclosure_bundle_id },
        requested: requested === true })); } catch {}
    },
  };
}
export function fullReviewIntent(journeyId, draftScope) {
  return { request_id: crypto.randomUUID(), journey_id: journeyId, draft_scope: { ...draftScope } };
}
export function createFullReviewIntentConsumer({ apply, storage = globalThis.sessionStorage }) {
  let lastRequest = null;
  try { lastRequest = storage?.getItem(LAST_REQUEST_KEY); } catch {}
  return (intent, record) => {
    if (!sameScope(intent, record) || !uuid.test(intent.request_id) || intent.request_id === lastRequest) return false;
    lastRequest = intent.request_id;
    try { storage?.setItem(LAST_REQUEST_KEY, lastRequest); } catch {}
    apply();
    return true;
  };
}
