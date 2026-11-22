// Pure synthetic state matrices for native desktop onboarding and browser task
// pages. Shared by static capture and independent CI inventory without Node I/O.
import { FIXED_NOW } from './screen-matrix.mjs';

const GENERATION = '11111111-1111-4111-8111-111111111111';
const OPERATION_ID = '22222222-2222-4222-8222-222222222222';
const JOURNEY_ID = '33333333-3333-4333-8333-333333333333';
const TASK_ID = '00000001-0000-4000-8000-000000000000';
const ATTEMPT_ID = '0199b234-5678-7000-8000-000000000002';
const EXTENSION_ID = 'a'.repeat(32);

const baseFacts = (verified = []) => Object.fromEntries(
  ['browser_access', 'installation', 'account', 'configuration', 'enrollment', 'pairing', 'activation', 'analysis']
    .map(name => [name, verified.includes(name) ? 'verified' : 'missing']),
);

const creatorLabel = Object.freeze({
  creator_id: 'creator-selected',
  display_name: 'Sample Studio',
  username: 'sample.studio',
});

const providerChoices = Object.freeze({
  google: true,
  'microsoft-personal': true,
  telegram: false,
  tiktok: false,
  default: true,
});

function desktopSnapshot(overrides = {}) {
  return {
    profile: 'desktop-onboarding.v2',
    kind: 'snapshot',
    journey_id: JOURNEY_ID,
    generation: GENERATION,
    revision: 1,
    scope_digest: 'a'.repeat(64),
    expires_at: 2000000000,
    facts: baseFacts(),
    next_task: 'browser-access',
    local_ready: false,
    full_ready: false,
    pending_operations: [],
    allowed_actions: [],
    reason: 'none',
    analysis_readiness: { availability: 'unauthorized', creator_account_id: null, value: null },
    ...overrides,
  };
}

function desktopState(snapshotOverrides, stateOverrides = {}) {
  return Object.freeze({
    connected: true,
    pending: null,
    error: null,
    acting: null,
    provider: null,
    choosingAccount: false,
    browserLaunchPending: false,
    authentication: null,
    continuing: false,
    deliberate: false,
    ...stateOverrides,
    snapshot: snapshotOverrides === null ? null : desktopSnapshot(snapshotOverrides),
  });
}

function signInProjection(overrides = {}) {
  return {
    operation_id: null,
    browser_operation_id: null,
    state: 'selection-required',
    choices: { ...providerChoices },
    actions: { select: true, cancel: false, open_browser: false, switch_account: false },
    authentication_attempt: null,
    ...overrides,
  };
}

function pendingOp(action, state = 'accepted', reason = 'none') {
  return { operation_id: OPERATION_ID, action, state, reason };
}

function localAuth(overrides = {}) {
  return {
    checked: true,
    enrolled: false,
    authenticated: false,
    busy: false,
    checking: false,
    recoveringStatus: false,
    error: null,
    access: null,
    attempt: null,
    ...overrides,
  };
}

const finishSetupVerifiedFacts = baseFacts(['browser_access', 'installation', 'account', 'configuration']);
const authenticatedLocal = localAuth({ enrolled: true, authenticated: true });

export const DESKTOP_ONBOARDING_STATES = Object.freeze({
  loading: desktopState(null, { connected: false }),

  browser_access_needed: desktopState({
    journey_id: null,
    next_task: 'browser-access',
    allowed_actions: ['browser.access'],
  }),
  browser_access_connecting: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'accepted')],
  }, { pending: OPERATION_ID, acting: 'browser.access' }),
  browser_access_in_chrome: desktopState({
    pending_operations: [pendingOp('browser.access', 'waiting')],
  }),
  browser_access_reconnecting: desktopState({
    pending_operations: [pendingOp('browser.access', 'waiting')],
  }, { connected: false, error: 'status_unavailable' }),
  browser_access_chrome_unavailable: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'chrome_unavailable')],
  }, { error: 'chrome_unavailable' }),
  browser_access_extension_unavailable: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'extension_unavailable')],
  }, { error: 'extension_unavailable' }),
  browser_access_profile_mismatch: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'profile_mismatch')],
  }, { error: 'profile_mismatch' }),
  browser_access_account_mismatch: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'account_mismatch')],
  }, { error: 'account_mismatch' }),
  browser_access_needs_account: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'browser_needs_account')],
  }, { error: 'browser_needs_account' }),
  browser_access_needs_consent: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'browser_needs_consent')],
  }, { error: 'browser_needs_consent' }),
  browser_access_needs_permission: desktopState({
    next_task: 'browser-access',
    pending_operations: [pendingOp('browser.access', 'waiting', 'browser_needs_permission')],
  }, { error: 'browser_needs_permission' }),

  account_connection_providers: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['account.connect'],
    authentication: signInProjection(),
  }),
  account_sign_in_opening: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['account.connect'],
    authentication: signInProjection(),
  }, { acting: 'account.connect', provider: 'google' }),
  account_sign_in_in_progress: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['auth.switch', 'auth.cancel'],
    pending_operations: [pendingOp('auth.select', 'waiting')],
    authentication: signInProjection({
      operation_id: OPERATION_ID,
      browser_operation_id: OPERATION_ID,
      state: 'selected',
      actions: { select: false, cancel: true, open_browser: true, switch_account: true },
      authentication_attempt: { authentication_attempt_id: ATTEMPT_ID, provider: 'google', mode: 'sign-in' },
    }),
  }, { pending: OPERATION_ID }),
  account_confirm: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['account.confirm', 'auth.switch'],
    pending_operations: [pendingOp('account.connect', 'waiting')],
    decision: {
      operation_id: OPERATION_ID,
      action: 'connect-account',
      creator_id: 'creator-selected',
      computer_name: 'Studio computer',
      expires_at: '2030-01-01T00:00:00Z',
    },
    decision_status: { operation_id: OPERATION_ID, state: 'confirmation-required' },
    authentication: signInProjection({
      operation_id: OPERATION_ID,
      browser_operation_id: OPERATION_ID,
      state: 'admitted',
      actions: { select: false, cancel: true, open_browser: false, switch_account: true },
      authentication_attempt: { authentication_attempt_id: ATTEMPT_ID, provider: 'google', mode: 'sign-in' },
    }),
  }),
  account_confirmed: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    decision_status: { operation_id: OPERATION_ID, state: 'accepted' },
    pending_operations: [pendingOp('account.connect', 'waiting')],
  }),
  account_confirmation_unknown: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    pending_operations: [pendingOp('account.confirm', 'outcome_unknown', 'operation_unconfirmed')],
  }),
  account_sign_in_did_not_finish: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['auth.select'],
    pending_operations: [pendingOp('auth.select', 'outcome_unknown', 'operation_unconfirmed')],
    authentication: signInProjection({
      operation_id: OPERATION_ID,
      browser_operation_id: OPERATION_ID,
      state: 'reauthentication-required',
    }),
  }, { pending: OPERATION_ID, provider: 'google' }),
  reauthentication_required: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: ['account.connect'],
  }, { connected: false, error: 'authorization_unavailable' }),
  sign_in_expired: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: [],
    authentication: signInProjection({
      operation_id: OPERATION_ID,
      browser_operation_id: OPERATION_ID,
      state: 'expired',
      actions: { select: false, cancel: false, open_browser: false, switch_account: false },
    }),
  }),
  outcome_unknown: desktopState({
    next_task: 'account-connection',
    facts: baseFacts(['browser_access']),
    creator_label: creatorLabel,
    allowed_actions: [],
    pending_operations: [pendingOp('account.connect', 'outcome_unknown')],
  }, { error: 'operation_unconfirmed' }),

  finish_secure_app_setup: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: false, authenticated: false }) }),
  finish_secure_app_sign_in: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: true, authenticated: false }) }),
  finish_passkey_creating: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
  }, { authentication: localAuth({ busy: true, attempt: 'create' }) }),
  finish_passkey_sign_in_failed: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
  }, { authentication: localAuth({ enrolled: true, error: 'passkey_unconfirmed', attempt: 'sign-in' }) }),
  finish_getting_ready: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ checked: false, enrolled: true, authenticated: true }) }),
  finish_connect_extension: desktopState({
    next_task: 'finish-setup',
    facts: baseFacts(['browser_access', 'installation', 'account', 'configuration', 'enrollment']),
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: authenticatedLocal }),
  finish_connecting_extension: desktopState({
    next_task: 'finish-setup',
    facts: baseFacts(['browser_access', 'installation', 'account', 'configuration', 'enrollment']),
    creator_label: creatorLabel,
    pending_operations: [pendingOp('browser.pair', 'waiting')],
  }, { authentication: authenticatedLocal, browserLaunchPending: true }),
  finish_connecting_extension_reopen: desktopState({
    next_task: 'finish-setup',
    facts: baseFacts(['browser_access', 'installation', 'account', 'configuration', 'enrollment']),
    creator_label: creatorLabel,
    pending_operations: [pendingOp('browser.pair', 'waiting')],
  }, { authentication: authenticatedLocal, browserLaunchPending: false }),
  finish_preparing_dipsy: desktopState({
    next_task: 'finish-setup',
    facts: baseFacts(['browser_access', 'installation', 'account']),
    creator_label: creatorLabel,
    allowed_actions: ['setup.finalize'],
  }),

  passkey_status_recovering: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, recoveringStatus: true, error: 'passkey_status_network' }) }),
  passkey_status_unavailable: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, error: 'passkey_status_unavailable' }) }),
  passkey_status_session: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, error: 'passkey_status_session' }) }),
  passkey_status_forbidden: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, error: 'passkey_status_forbidden' }) }),
  passkey_status_invalid: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, error: 'passkey_status_invalid' }) }),
  passkey_sign_in_required: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: true, error: 'passkey_sign_in_required' }) }),
  passkey_start_unavailable: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: true, error: 'passkey_start_unavailable' }) }),
  passkey_unconfirmed: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: true, error: 'passkey_unconfirmed' }) }),
  passkey_access_renewing: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, access: 'renewing' }) }),
  passkey_access_expired: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, access: 'expired' }) }),
  passkey_access_removed: desktopState({
    next_task: 'finish-setup',
    facts: finishSetupVerifiedFacts,
    creator_label: creatorLabel,
    allowed_actions: ['browser.pair'],
  }, { authentication: localAuth({ enrolled: null, access: 'removed' }) }),
});

function browserTaskPayload(purpose = 'browser.access') {
  const fixedMs = Date.parse(FIXED_NOW);
  const value = {
    profile: 'browser-task.v1',
    extension_id: EXTENSION_ID,
    ceremony: null,
    entry_source: 'desktop',
    participant: null,
    creator_account_id: purpose === 'browser.access' ? null : 'synthetic-creator',
    task: {
      task_id: TASK_ID,
      journey_id: JOURNEY_ID,
      operation_id: OPERATION_ID,
      admission_generation: GENERATION,
      scope_digest: 'a'.repeat(64),
      source_revision: 0,
      state: 'accepted',
      purpose,
      expires_at: fixedMs / 1000 + 300,
    },
  };
  if (purpose === 'browser.authentication') {
    value.ceremony = {
      entry_url: 'https://provider.example/onboarding/browser-ceremonies/' + 'A'.repeat(43),
      ceremony_id: ATTEMPT_ID,
      attempt_id: 'A'.repeat(43),
      completion: { mode: 'extension-auth-flow', return_uri: `https://${EXTENSION_ID}.chromiumapp.org/browser-ceremony/v1/complete` },
      expires_at: new Date(fixedMs + 300_000).toISOString(),
    };
  }
  return value;
}

const taskState = (state, purpose = 'browser.access') => Object.freeze({
  state,
  payload: purpose === null ? null : browserTaskPayload(purpose),
});

export const BROWSER_TASK_STATES = Object.freeze({
  loading: taskState('loading', 'browser.access'),
  unavailable: taskState('unavailable', 'browser.access'),
  extension_disconnected: taskState('extension_disconnected', 'browser.access'),
  profile_mismatch: taskState('profile_mismatch', 'browser.access'),
  account_mismatch: taskState('account_mismatch', 'browser.access'),
  auth_idle: taskState('auth_idle', 'browser.authentication'),
  auth_opening: taskState('auth_opening', 'browser.authentication'),
  auth_checking: taskState('auth_checking', 'browser.authentication'),
  auth_closed: taskState('auth_closed', 'browser.authentication'),
  auth_unconfirmed: taskState('auth_unconfirmed', 'browser.authentication'),
  auth_access_required: taskState('auth_access_required', 'browser.authentication'),
  pairing_connecting: taskState('pairing_connecting', 'browser.pairing-confirmation'),
  pairing_checking: taskState('pairing_checking', 'browser.pairing-confirmation'),
  pairing_saved: taskState('pairing_saved', 'browser.pairing-confirmation'),
  pairing_unconfirmed: taskState('pairing_unconfirmed', 'browser.pairing-confirmation'),
  pairing_access_required: taskState('pairing_access_required', 'browser.pairing-confirmation'),
  task_expired: taskState('task_expired', 'browser.access'),
  task_changed: taskState('task_changed', 'browser.access'),
  task_completed: taskState('task_completed', 'browser.access'),
  service_unavailable: taskState('service_unavailable', 'browser.access'),
  service_unavailable_no_payload: taskState('service_unavailable', null),
  status_reconnecting: taskState('status_reconnecting', 'browser.access'),
  needs_consent: taskState('needs-consent', 'browser.access'),
  needs_permission: taskState('needs-permission', 'browser.access'),
  needs_account: taskState('needs-account', 'browser.access'),
  ready: taskState('ready', 'browser.access'),
  paused: taskState('paused', 'browser.access'),
  reported: taskState('reported', 'browser.access'),
});
