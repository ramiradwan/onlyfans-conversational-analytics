// Story-harness data only. Never imported by a product entry point.
const operationId = '11111111-1111-4111-8111-111111111111';
const browserOperationId = '22222222-2222-4222-8222-222222222222';
export const activationDecision = {
  operation_id: operationId, action: 'activate-complimentary', expires_at: '2030-01-01T00:00:00Z',
  offer: { offer_version_id: 'visual-test-offer', description: 'Full conversation analytics', funding: 'complimentary-pilot',
    pilot_credit_cost: 1, capability: 'analysis-run', licensed_major_version: 2,
    compatible_artifact_family: 'analysis-artifact', update_rights: true, fallback_major_versions: [1] },
};

export function activationProjection(state) {
  const selecting = state === 'selection';
  const waiting = state === 'waiting';
  const review = state === 'review';
  const authentication = selecting || waiting || review ? {
    operation_id: operationId, browser_operation_id: waiting ? browserOperationId : null,
    state: selecting ? 'selection-required' : waiting ? 'selected' : 'admitted',
    choices: { google: true, 'microsoft-personal': true, default: true },
    actions: { select: selecting, cancel: true, open_browser: waiting, switch_account: !selecting },
    authentication_attempt: selecting ? null : { authentication_attempt_id: '019a53e1-0000-7000-8000-000000000001', provider: 'google', mode: 'sign-in' },
  } : null;
  return { next_task: 'activation', decision: review ? activationDecision : null,
    decision_status: { operation_id: operationId, state: state === 'accepted' ? 'accepted'
      : state === 'unavailable' ? 'unavailable' : review ? 'confirmation-required' : 'awaiting-authentication' },
    authentication, allowed_actions: review ? ['activation.confirm', 'auth.switch', 'auth.cancel']
      : selecting ? ['auth.select', 'auth.cancel'] : waiting ? ['auth.switch', 'auth.cancel'] : [],
    pending_operations: [{ operation_id: operationId, action: 'activation.review', state: 'accepted', reason: 'none' }] };
}

export async function publishActivation(page, state) {
  await page.evaluate(projection => window.__workspaceFixture.desktopSnapshot(projection), activationProjection(state));
}
