const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
export const NATIVE_LAUNCH_KEY = 'onboarding_native_launch_v1';
export const NATIVE_LAUNCH_TTL_MS = 10 * 60 * 1000;

export function workspaceAppLink(journeyId) {
  if (!UUID.test(journeyId)) throw Error('workspace_reference_invalid');
  return `ofca://onboarding/?journey=${journeyId}`;
}
