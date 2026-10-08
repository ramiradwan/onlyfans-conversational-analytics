import { SURFACE_STATES } from '../../extension/qualification/surface-states.mjs';

export const DYNAMIC_VIEWS = ['home', 'analytics', 'inbox', 'settings', 'passkey', 'graph', 'popup', 'setup', 'options', 'provisioning', 'production-boot'];
export function dynamicCases(only = DYNAMIC_VIEWS, focused = false) {
  return only.flatMap(view => {
    if (!DYNAMIC_VIEWS.includes(view)) throw new Error('Unknown transition view');
    const widths = view === 'popup' ? [320, 390] : view === 'inbox' ? [390, 820, 1440]
      : ['passkey', 'production-boot'].includes(view) ? [390, 1366, 1440]
        : ['setup', 'provisioning'].includes(view) ? [390, 480, 1440] : [390, 1440];
    return widths.flatMap(width => ['light', 'dark'].flatMap(mode => [1, 1.25].flatMap(fontScale => ['reduce', 'no-preference']
      .filter(motion => !focused || (width === widths[0] && mode === 'light' && fontScale === 1 && motion === 'reduce'))
      .map(motion => ({ view, width, mode, fontScale, motion })))));
  });
}
const permutations = values => values.length ? values.flatMap(value => permutations(values.filter(item => item !== value)).map(rest => [value, ...rest])) : [[]];

// Independently enumerate the full ordered driver contract, including repeated
// states. Runtime receipts are recorded by the actual driver, never this list.
export function dynamicSequence(view, width) {
  if (!DYNAMIC_VIEWS.includes(view)) throw new Error('Unknown transition view');
  const steps = [];
  const add = (...values) => steps.push(...values);
  if (['popup', 'setup', 'options'].includes(view)) {
    const cases = Object.entries(SURFACE_STATES).filter(([, state]) => state.surface === view);
    for (const prior of [cases[0], cases.at(-1)]) for (const [name, state] of cases) {
      add(`${prior[0]}:${name}`);
      if (state.dialog) add(`${name}:dialog`);
    }
    for (const field of ['phase', 'pairing', 'commercial']) add(`unknown:${field}`);
    for (const value of [null, 0, Number.MAX_SAFE_INTEGER]) add(`preview-counts:${value ?? 'null'}`);
    return steps;
  }
  if (view === 'production-boot') return ['delayed-renderer-and-fonts', 'request-error:Sign in with passkey', 'request-error:Set up a passkey'];
  if (view === 'provisioning') {
    for (const flag of ['malformed', 'expired', 'unavailable', 'identityUnavailable']) add(`controller:${flag}`, `controller:${flag}:recovery`);
    for (const stage of ['needs_terms', 'needs_full', 'needs_site_access', 'needs_account', 'ready_to_pair', 'paired']) add(`extension:${stage}`);
    add('extension:missing', 'invalid-code', 'valid-code', 'claim:pending', 'claim:confirmed');
    for (const identity of ['absent', 'changed', 'present']) add(`identity:${identity}`);
    add('account:confirm', 'recovery:open', 'recovery:close');
    for (const reason of ['size', 'encoding', 'profile', 'schema', 'device', 'consumed', 'binding_acquisition_unavailable', 'hosted_origin_unavailable', 'hosted_unavailable', 'installation_key_unavailable', 'membership_reference_unavailable', 'candidate_resolution_conflict', 'grant_verification_refused', 'claim_already_consumed', 'claim_refused', 'incomplete_grant_set', 'membership_refresh_unavailable', 'unknown']) add(`approval:refusal:${reason}`);
    add('approval:pending', 'finalization:pending', 'finalization:completed', 'finalization:response-lost-reconciled');
    return steps;
  }
  add('loading');
  if (view === 'passkey') {
    for (const action of ['Sign in with passkey', 'Set up a passkey']) for (const outcome of ['NotAllowedError', 'AbortError', 'Error', 'success']) add(`${action}:${outcome}:pending`, `${action}:${outcome}`);
    return steps;
  }
  if (view === 'analytics') {
    for (const prior of ['loading', 'model']) for (const state of ['loading', 'building', 'unavailable', 'baseline', 'model', 'error']) add(`${prior}:${state}`);
    add('tone:Table', 'tone:Chart', 'dates:open', 'dates:invalid', 'dates:close');
    return steps;
  }
  if (view === 'graph') {
    for (const prior of ['pending', 'current']) for (const state of ['current', 'pending', 'unavailable', 'degraded']) add(`${prior}:${state}`);
    return steps;
  }
  for (const state of ['fresh', 'syncing', 'populated', 'fresh', 'populated']) add(`snapshot:${state}`);
  // The initial settings requests follow the mounted section order. The later
  // response-order matrix still exercises all 24 permutations independently.
  const pending = view === 'home' ? ['activation.readiness'] : view === 'inbox' ? ['message.getPage'] : ['pairing.pins', 'activation.readiness', 'history.get', 'vault.get'];
  for (const key of pending) add(`response:${key}`);
  for (const state of ['reconnecting', 'disconnected', 'connected', 'error', 'connected']) add(`bridge:${state}`, `grace:${state}:2999`, `grace:${state}:3000`);
  if (view === 'inbox') add('message:pending', 'message:error');
  if (view === 'settings') {
    const keys = ['pairing.pins', 'history.get', 'activation.readiness', 'vault.get'];
    for (const order of permutations(keys)) for (const key of order) add(`response-order:${order.join(',')}:${key}`);
    for (const key of keys) add(`response-error:${key}`);
    for (const capture of ['active', 'paused', 'off']) for (const access of ['granted', 'needs_approval', 'reload_required']) for (const permission of ['granted', 'missing']) for (const review of [false, true]) add(`browser:${capture}:${access}:${permission}:${review}`);
    for (const state of ['pending', 'delivered', '9999', '10000', 'authoritative', 'bridge-lost', 'bridge-lost-grace', 'bridge-return']) add(`control:${state}`);
    add('disconnect:open', 'disconnect:cancel');
  }
  if (view === 'home' || view === 'inbox') {
    for (const field of ['display_name', 'latest_message.text', 'coverage.reason_code']) add(`long:conversation:${field}`);
    for (const field of ['coverage', 'projection', 'live_freshness', 'catchup_freshness']) add(`long:${field}:reason`);
    for (const value of [0, null, 9_999_999, Number.MAX_SAFE_INTEGER]) add(`counts:${value ?? 'null'}`);
    for (const phase of ['not_started', 'discovering', 'backfilling', 'complete', 'blocked', 'paused']) add(`coverage:${phase}`);
  }
  if (view === 'home') {
    for (const status of ['connected', 'stale', 'disconnected', 'degraded', 'config-mismatch']) add(`agent:${status}`);
    for (const state of ['idle', 'connecting', 'handshaking', 'connected']) add(`connection:${state}`);
    for (const code of ['unauthorized', 'unsupported_feature', 'unknown']) for (const fatal of [false, true]) add(`protocol:${code}:${fatal}`, 'protocol:recovered');
    add('read-model:degraded', 'read-model:resyncing', 'read-model:realtime');
    for (const authority of ['required', 'active', 'unavailable']) for (const admission of ['admitted', 'blocked']) add(`readiness:${authority}:${admission}`);
    for (const control of ['Details', '/^Status:/', ...(width === 390 ? ['Open navigation'] : [])]) add(`overlay:${control}:open`, `overlay:${control}:close`);
  }
  if (view === 'inbox') {
    for (const prior of ['empty', 'populated']) for (const state of ['pending', 'empty', 'success', 'error', 'long', 'attachment', 'prepend']) add(`message:${prior}:${state}`);
    if (width < 1200) add('inbox:back');
  }
  if (view !== 'settings') return steps;
  for (const desired of ['not_started', 'running', 'paused', 'revoked']) for (const effective of ['not_applied', desired]) add(`history:${desired}:${effective}`);
  for (const authority of ['required', 'active', 'unavailable']) for (const admission of ['admitted', 'blocked']) add(`activation:${authority}:${admission}`);
  for (const policy of ['disabled', 'finite', 'indefinite_until_delete']) for (const capable of [false, true]) for (const deletion of [null, 'pending', 'incomplete', 'complete']) add(`vault:${policy}:${capable}:${deletion}`);
  add('archive:open', 'archive:invalid', 'archive:indefinite', 'archive:cancel', 'delete:open', 'delete:pending', 'delete:error', 'activation:open', 'activation:invalid', 'activation:cancel');
  for (const outcome of ['refusal', 'network', 'success']) for (const stage of ['open', 'pending', 'response', 'readiness']) add(`redemption:${outcome}:${stage}`);
  for (const outcome of ['failure', 'success']) { add(`export:${outcome}:pending`, `export:${outcome}:response`); for (const stage of ['open', 'pending', 'response']) add(`deletion:${outcome}:${stage}`); }
  for (const stage of ['unavailable', 'needs_terms', 'paused', 'needs_full', 'needs_site_access', 'needs_account', 'ready_to_pair', 'pairing', 'paired']) add(`port:${stage}`);
  add('pairing:open', 'pairing:pending', 'pairing:299999', 'pairing:300000');
  for (const outcome of ['declined', 'cancelled', 'confirmed', 'refused']) add(`pairing:${outcome}:start`, `pairing:${outcome}:comparison`, `pairing:${outcome}:${['confirmed', 'refused'].includes(outcome) ? 'extension' : 'action'}`, `pairing:${outcome}:result`);
  return steps;
}
