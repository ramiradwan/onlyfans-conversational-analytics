// Synthetic browser-task coordinates; excluded from production bundles.
export const id = index => `${String(index).padStart(8, '0')}-0000-4000-8000-000000000000`;
export function payload(now = Date.now()) {
  return { profile: 'browser-task.v1', extension_id: 'a'.repeat(32), ceremony: null, entry_source: 'desktop', participant: null, creator_account_id: null,
    task: { task_id: id(1), journey_id: id(2), operation_id: id(3), admission_generation: id(4),
      scope_digest: 'a'.repeat(64), source_revision: 0, state: 'accepted', purpose: 'browser.access', expires_at: now / 1000 + 30 } };
}
export function observation(task = payload().task) {
  return { profile: 'browser-task-observation.v1', task_id: task.task_id, journey_id: task.journey_id,
    admission_generation: task.admission_generation, extension_id: 'a'.repeat(32), instance_id: id(5), epoch: id(6),
    revision: 1, account_generation: 1, consent_generation: 1, draft_scope_id: id(7), disclosure_bundle_id: 'b'.repeat(64),
    state: 'needs-consent', creator_account_id: null };
}
