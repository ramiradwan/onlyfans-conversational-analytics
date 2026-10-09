// A request to return to one setup tab, never proof that the app is running.
export const NATIVE_LAUNCH_KEY = 'onboarding_native_launch_v1';
export const NATIVE_RETURN_TYPE = 'ofca.workspace.launch-return.v1';
export const NATIVE_DISCOVER_TYPE = 'ofca.workspace.launch-discover.v1';
export const NATIVE_FOCUS_TYPE = 'ofca.workspace.launch-focus.v1';
export const NATIVE_LAUNCH_TTL_MS = 10 * 60 * 1000;
export const NATIVE_RETURN_PATH = '/provisioning/native-return';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

export function workspaceAppLink(journeyId) {
  if (!UUID.test(journeyId)) throw Error('workspace_reference_invalid');
  return `ofca://onboarding?journey=${journeyId}`;
}

export function nativeReturnJourney(value, localOrigin) {
  try {
    const url = new URL(value);
    const journey = url.hash.startsWith('#journey=') ? url.hash.slice(9) : '';
    return UUID.test(journey) && url.href === `${localOrigin}${NATIVE_RETURN_PATH}#journey=${journey}` ? journey : null;
  } catch { return null; }
}
