const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

/** Cross-site return has no state. One same-origin check admits the existing cookie. */
export async function resumeOnboarding({ fetch, location, status, signal, history = globalThis.history,
  loadValidation = () => Promise.all([import('./provisioning.js'), import('/provisioning/onboarding/json.mjs')]) }) {
  const hint = location.hash.startsWith('#journey=') ? location.hash.slice(9) : null;
  if (location.hash && (hint === null || !UUID.test(hint))) {
    status.textContent = 'Open the desktop app to continue setup.';
    return false;
  }
  let runtime = false;
  const read = (path) => fetch(path, {
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
    headers: { Accept: 'application/json', ...(hint ? { 'X-Onboarding-Journey': hint } : {}) },
  });
  try {
    let response = await read('/api/v1/provisioning/state');
    if (response.status === 404) { runtime = true; response = await read('/api/v1/onboarding/state'); }
    if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json'
      || !response.headers.get('X-Onboarding-Capabilities')?.split(',').map((part) => part.trim()).includes('local-onboarding.v1')) {
      throw new Error('Setup context unavailable');
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > 4096) throw new Error('Invalid setup context');
    const [{ parseBrainOnboardingState }, { parseOnboardingJson }] = await loadValidation();
    const state = parseBrainOnboardingState(parseOnboardingJson(text));
    // This shell never renders facts or accepts authority from the fragment.
    // Only the authenticated response can choose its bound journey reference.
    if (!state || state.profile !== 'local-onboarding-state.v1' || state.source !== 'brain'
      || state.kind !== 'snapshot' || !UUID.test(state.journey_id) || (hint && state.journey_id !== hint)) {
      throw new Error('Invalid setup context');
    }
    const target = `${runtime ? '/' : '/provisioning'}#journey=${state.journey_id}`;
    if (runtime) location.replace(target);
    else {
      // The 401 shell is already at /provisioning. A fragment-only replace
      // would keep that document, so request the authenticated local document.
      history.replaceState(null, '', target);
      location.reload();
    }
    return true;
  } catch {
    status.textContent = 'Open the desktop app to continue setup.';
    return false;
  }
}

if (typeof document !== 'undefined' && document.querySelector('main[data-onboarding-resume]')) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 10_000);
  void resumeOnboarding({ fetch: globalThis.fetch.bind(globalThis), location: window.location,
    status: document.getElementById('resume-status'), signal: controller.signal }).finally(() => clearTimeout(deadline));
}
