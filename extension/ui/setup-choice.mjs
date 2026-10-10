// Scoped presentation intent. Neither saved choices nor drafts authorize analytics.
const KEY = 'onboarding-mode-review-v1';
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const scope = (record) => record && uuid.test(record.journey_id) && uuid.test(record.draft_scope?.scope_id)
  && /^[0-9a-f]{64}$/u.test(record.draft_scope?.disclosure_bundle_id)
  ? `${record.journey_id}:${record.draft_scope.scope_id}:${record.draft_scope.disclosure_bundle_id}` : null;

export function createSetupChoice(storage = globalThis.sessionStorage) {
  return {
    read(record) {
      try {
        const value = JSON.parse(storage?.getItem(KEY) ?? 'null');
        if (scope(record) && value?.scope === scope(record) && ['preview', 'full'].includes(value.mode)
          && ['review', 'access'].includes(value.stage)) return { mode: value.mode, stage: value.stage };
        storage?.removeItem(KEY);
      } catch {}
      return null;
    },
    write(record, value) {
      try {
        if (!value) { storage?.removeItem(KEY); return; }
        if (scope(record) && ['preview', 'full'].includes(value.mode) && ['review', 'access'].includes(value.stage)) {
          storage?.setItem(KEY, JSON.stringify({ scope: scope(record), mode: value.mode, stage: value.stage }));
        }
      } catch {}
    },
  };
}
