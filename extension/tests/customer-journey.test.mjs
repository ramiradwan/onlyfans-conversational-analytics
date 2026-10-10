import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CUSTOMER_STATES,
  deriveCustomerJourney,
  probeDesktopRuntime,
} from '../runtime/customer-journey.mjs';

function status({ mode = 'full', phase = 'identity', transport = 'disconnected', sleeping = false } = {}) {
  return {
    consent: { mode },
    phase,
    delivery: { transport_state: transport, browser_tab_sleeping: sleeping },
  };
}

const pairing = (state) => ({ state, comparison_code: null });
const readiness = (commercial_authority, analysis_admission = 'blocked') => ({
  commercial_authority,
  analysis_admission,
});

test('Preview remains independent of the desktop app', () => {
  const result = deriveCustomerJourney({
    status: status({ mode: 'preview', phase: 'preview' }),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: false,
  });
  assert.equal(result.id, CUSTOMER_STATES.PREVIEW_AVAILABLE);
  assert.equal(result.primaryAction, 'review_full');
  assert.equal(result.primaryLabel, 'Review Full analytics');
});

test('analytics off reopens the mode choice only when it is available', () => {
  const available = deriveCustomerJourney({
    status: status({ mode: 'off', phase: 'off' }),
    pairing: pairing('unpaired'),
    modeChoiceAvailable: true,
  });
  assert.equal(available.id, CUSTOMER_STATES.ANALYTICS_OFF);
  assert.equal(available.primaryAction, 'choose_mode');
  assert.equal(available.primaryLabel, 'Set up Preview');

  const unavailable = deriveCustomerJourney({
    status: status({ mode: 'off', phase: 'off' }),
    pairing: pairing('unpaired'),
  });
  assert.equal(unavailable.id, CUSTOMER_STATES.ANALYTICS_OFF);
  assert.equal(unavailable.primaryAction, null);
});

test('paused analytics offer resume only when no review is pending', () => {
  const resumable = deriveCustomerJourney({
    status: status({ mode: 'paused', phase: 'paused' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    resumeAvailable: true,
  });
  assert.equal(resumable.id, CUSTOMER_STATES.PAUSED);
  assert.equal(resumable.primaryAction, 'resume');
  assert.doesNotMatch(resumable.title, /Preview/);

  const reviewFirst = deriveCustomerJourney({
    status: status({ mode: 'paused', phase: 'paused' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
  });
  assert.equal(reviewFirst.id, CUSTOMER_STATES.PAUSED);
  assert.equal(reviewFirst.primaryAction, null);
  assert.match(reviewFirst.body, /Review the updated information/);

  const reviewReady = deriveCustomerJourney({
    status: status({ mode: 'paused', phase: 'paused' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    modeChoiceAvailable: true,
  });
  assert.equal(reviewReady.primaryAction, 'choose_mode');
});

test('first Full attempt explains that the desktop app is required', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: false,
    desktopDownloadAvailable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.DESKTOP_APP_NEEDED);
  assert.equal(result.primaryAction, 'open_desktop');
  assert.equal(result.secondaryAction, 'install_desktop');
  assert.match(result.body, /Open the desktop app/);
  assert.doesNotMatch(result.body, /Preview/);

  const noDownload = deriveCustomerJourney({
    status: status(),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: false,
  });
  assert.equal(noDownload.primaryAction, 'open_desktop');
  assert.doesNotMatch(noDownload.body, /Preview/);
});

test('returning paired user gets an unconfirmed connection without assuming the app stopped', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('paired'),
    desktopRuntimeReachable: false,
  });
  assert.equal(result.id, CUSTOMER_STATES.DESKTOP_APP_UNAVAILABLE);
  assert.equal(result.primaryAction, 'open_desktop');
  assert.equal(result.title, 'Desktop connection not confirmed');
  assert.doesNotMatch(result.title + result.body, /app.*(closed|stopped|unavailable)/i);
});

test('a confirmed active connection attempt differs from an unconfirmed result', () => {
  const input = { status: status({ transport: 'authenticating' }), pairing: pairing('paired'), desktopRuntimeReachable: false };
  const pending = deriveCustomerJourney(input);
  assert.equal(pending.title, 'Connecting to the desktop app');
  assert.equal(pending.tone, 'progress');
  assert.equal(pending.primaryAction, null);
  const unknown = deriveCustomerJourney({ ...input, status: status(), connectionRecovery: 'exhausted' });
  assert.equal(unknown.title, 'Desktop connection not confirmed');
  assert.equal(unknown.tone, 'warning');
  assert.notEqual(unknown.title, 'Connecting to the desktop app');
});

test('a missing pairing status never instructs the customer to sign in to OnlyFans', () => {
  const model = { status: status(), pairing: pairing('unavailable'), desktopRuntimeReachable: false };
  const recovering = deriveCustomerJourney({ ...model, connectionRecovery: 'retrying' });
  assert.equal(recovering.title, 'Reconnecting to the extension');
  assert.equal(recovering.primaryAction, null);
  const exhausted = deriveCustomerJourney({ ...model, connectionRecovery: 'exhausted' });
  assert.equal(exhausted.title, 'Connection status unavailable');
  assert.equal(exhausted.primaryLabel, 'Try again');
  assert.doesNotMatch(exhausted.title + exhausted.body, /sign in|enable extension|desktop app closed/i);
});

test('missing site permission cannot inherit the old Full-ready state', () => {
  for (const mode of ['preview', 'full']) {
    const output = deriveCustomerJourney({
      status: status({ mode, phase: 'permission_required', transport: 'authenticated' }),
      pairing: pairing('paired'), desktopRuntimeReachable: true,
      analysisReadiness: readiness('active', 'admitted'),
    });
    assert.notEqual(output.id, CUSTOMER_STATES.FULL_READY);
    assert.equal(output.title, 'Site access needed');
    assert.equal(output.primaryAction, 'restore_access');
  }
});

test('revoked analytics require a new mode choice, not permission reactivation', () => {
  const model = { status: status({ mode: 'revoked', phase: 'revoked', transport: 'authenticated' }),
    pairing: pairing('paired'), desktopRuntimeReachable: true, analysisReadiness: readiness('active', 'admitted') };
  const off = deriveCustomerJourney(model);
  assert.equal(off.id, CUSTOMER_STATES.ANALYTICS_OFF);
  assert.equal(off.title, 'Analytics off');
  assert.equal(off.primaryAction, null);
  assert.doesNotMatch(off.title + off.body, /allow site access|ready/i);
  assert.equal(deriveCustomerJourney({ ...model, modeChoiceAvailable: true }).primaryAction, 'choose_mode');
});

test('running desktop app with no usable creator context explains setup is incomplete', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('setup_incomplete'),
    desktopRuntimeReachable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.SETUP_INCOMPLETE);
  assert.equal(result.primaryLabel, 'Open creator account');
});

test('running desktop app advances an unpaired user to pairing', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.PAIRING_REQUIRED);
  assert.equal(result.primaryLabel, 'Connect extension');
});

test('pairing progress explains comparison and never claims success early', () => {
  const connecting = deriveCustomerJourney({
    status: status(), pairing: pairing('pairing'), desktopRuntimeReachable: true,
  });
  assert.equal(connecting.id, CUSTOMER_STATES.PAIRING_IN_PROGRESS);
  assert.match(connecting.body, /Keep this page open/);

  const compare = deriveCustomerJourney({
    status: status(), pairing: pairing('compare'), desktopRuntimeReachable: true,
  });
  assert.equal(compare.id, CUSTOMER_STATES.PAIRING_IN_PROGRESS);
  assert.match(compare.body, /same code/);
  assert.match(compare.body, /confirm there/);
});

test('pairing failure has a concrete retry path', () => {
  const result = deriveCustomerJourney({
    status: status(), pairing: pairing('pairing_failed'), desktopRuntimeReachable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.PAIRING_FAILED);
  assert.equal(result.primaryLabel, 'Try connection again');
});

test('pairing before the desktop app is ready names the desktop step first', () => {
  const result = deriveCustomerJourney({
    status: status(), pairing: pairing('desktop_not_ready'), desktopRuntimeReachable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.PAIRING_NOT_READY);
  assert.equal(result.tone, 'warning');
  assert.equal(result.primaryAction, 'open_desktop_settings');
  assert.equal(result.secondaryAction, 'pair');
  assert.match(result.body, /Connect extension/);
  assert.doesNotMatch(`${result.title} ${result.body}`, /fail|error|window/i);

  const stopped = deriveCustomerJourney({
    status: status(), pairing: pairing('desktop_not_ready'), desktopRuntimeReachable: false,
  });
  assert.equal(stopped.id, CUSTOMER_STATES.DESKTOP_APP_NEEDED);
});

test('authenticated local transport alone enters activation checking, never Full-ready', () => {
  const result = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
  });
  assert.notEqual(result.id, CUSTOMER_STATES.FULL_READY);
  assert.equal(result.id, CUSTOMER_STATES.ACTIVATION_CHECKING);
  assert.equal(result.title, 'Checking activation');
});

test('commercial activation required routes the customer to desktop activation without protocol fields', () => {
  const result = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('required'),
  });
  assert.equal(result.id, CUSTOMER_STATES.ACTIVATION_REQUIRED);
  assert.equal(result.title, 'Finish activating Full analytics');
  assert.equal(result.primaryAction, 'open_dashboard');
  assert.equal(result.primaryLabel, 'Open desktop app');
  assert.equal(result.secondaryAction, null);
  assert.equal(result.secondaryLabel, null);
  assert.match(result.body, /Settings in the desktop app/);
  assert.doesNotMatch(
    `${result.title} ${result.body} ${result.primaryLabel} ${result.secondaryLabel}`,
    /CapabilityLicense|package|seat[_ ]?id|license[_ ]?id|issuance[_ ]?id|JWS|proof challenge|installation key JKT|commercial exchange/i,
  );
});

test('commercial authority failure is distinct and recoverable', () => {
  const result = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('unavailable'),
  });
  assert.equal(result.id, CUSTOMER_STATES.ACTIVATION_UNAVAILABLE);
  assert.equal(result.primaryLabel, 'Check again');
  assert.equal(result.body, '');
  assert.doesNotMatch(result.body, /invalid license|saved data is unchanged/i);
});

test('commercial authority alone renders activation active but not Full-ready', () => {
  const result = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('active', 'blocked'),
  });
  assert.equal(result.id, CUSTOMER_STATES.ACTIVATION_ACTIVE);
  assert.equal(result.title, "New messages aren't being analyzed");
  assert.notEqual(result.id, CUSTOMER_STATES.FULL_READY);
  assert.equal(result.body, '');
  assert.equal(result.primaryAction, 'open_dashboard');
  assert.equal(result.primaryLabel, 'Open desktop app');
  assert.equal(result.secondaryAction, null);
});

test('sleeping OnlyFans tab has a direct recovery action after activation', () => {
  const result = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated', sleeping: true }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('active', 'blocked'),
  });
  assert.equal(result.id, CUSTOMER_STATES.FULL_UNAVAILABLE);
  assert.equal(result.title, 'Open OnlyFans to continue');
  assert.equal(result.primaryAction, 'open_creator_account');
  assert.equal(result.primaryLabel, 'Open OnlyFans');
  assert.equal(result.body, 'Your browser paused the OnlyFans tab.');
  assert.doesNotMatch(result.body, /resume automatically|ready/i);
});

test('Full is ready only after secure delivery, commercial authority, and analysis admission', () => {
  const notReady = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticating' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('active', 'admitted'),
  });
  assert.equal(notReady.id, CUSTOMER_STATES.FULL_UNAVAILABLE);

  const ready = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
    analysisReadiness: readiness('active', 'admitted'),
  });
  assert.equal(ready.id, CUSTOMER_STATES.FULL_READY);
  assert.equal(ready.primaryLabel, 'Open analysis');
  assert.equal(ready.title, 'Your analysis is ready');
});

test('desktop runtime probe reports an opened loopback socket and closes it', async () => {
  let closed = false;
  const reachable = await probeDesktopRuntime({
    timeoutMs: 50,
    webSocketFactory() {
      const socket = {
        close() { closed = true; },
        set onopen(handler) { queueMicrotask(handler); },
        set onerror(_handler) {},
        set onclose(_handler) {},
      };
      return socket;
    },
  });
  assert.equal(reachable, true);
  assert.equal(closed, true);
});

test('desktop runtime probe fails closed when no socket opens', async () => {
  const reachable = await probeDesktopRuntime({
    timeoutMs: 5,
    webSocketFactory() {
      return {
        close() {},
        set onopen(_handler) {},
        set onerror(_handler) {},
        set onclose(_handler) {},
      };
    },
  });
  assert.equal(reachable, false);
});
