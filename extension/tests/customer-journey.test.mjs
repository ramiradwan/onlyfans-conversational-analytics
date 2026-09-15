import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CUSTOMER_STATES,
  deriveCustomerJourney,
  probeDesktopRuntime,
} from '../runtime/customer-journey.mjs';

function status({ mode = 'full', phase = 'identity', transport = 'disconnected' } = {}) {
  return {
    consent: { mode },
    phase,
    delivery: { transport_state: transport },
  };
}

const pairing = (state) => ({ state, comparison_code: null });

test('Preview remains independent of the desktop app', () => {
  const result = deriveCustomerJourney({
    status: status({ mode: 'preview', phase: 'preview' }),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: false,
  });
  assert.equal(result.id, CUSTOMER_STATES.PREVIEW_AVAILABLE);
  assert.equal(result.primaryLabel, 'Activate Full analysis');
});

test('first Full attempt explains that the desktop app is required', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('unpaired'),
    desktopRuntimeReachable: false,
    desktopDownloadAvailable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.DESKTOP_APP_NEEDED);
  assert.equal(result.primaryLabel, 'Install desktop app');
  assert.match(result.body, /Preview can still be used without it/);
});

test('returning paired user gets a stopped-app recovery state', () => {
  const result = deriveCustomerJourney({
    status: status(),
    pairing: pairing('paired'),
    desktopRuntimeReachable: false,
  });
  assert.equal(result.id, CUSTOMER_STATES.DESKTOP_APP_UNAVAILABLE);
  assert.equal(result.primaryLabel, 'Retry connection');
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
  assert.equal(result.primaryLabel, 'Pair device');
});

test('pairing progress explains comparison and never claims success early', () => {
  const connecting = deriveCustomerJourney({
    status: status(), pairing: pairing('pairing'), desktopRuntimeReachable: true,
  });
  assert.equal(connecting.id, CUSTOMER_STATES.PAIRING_IN_PROGRESS);
  assert.match(connecting.body, /secure connection/);

  const compare = deriveCustomerJourney({
    status: status(), pairing: pairing('compare'), desktopRuntimeReachable: true,
  });
  assert.equal(compare.id, CUSTOMER_STATES.PAIRING_IN_PROGRESS);
  assert.match(compare.body, /six-digit code/);
});

test('pairing failure has a concrete retry path', () => {
  const result = deriveCustomerJourney({
    status: status(), pairing: pairing('pairing_failed'), desktopRuntimeReachable: true,
  });
  assert.equal(result.id, CUSTOMER_STATES.PAIRING_FAILED);
  assert.equal(result.primaryLabel, 'Try connection again');
});

test('Full is ready only after authenticated delivery', () => {
  const notReady = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticating' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
  });
  assert.equal(notReady.id, CUSTOMER_STATES.FULL_UNAVAILABLE);

  const ready = deriveCustomerJourney({
    status: status({ phase: 'full', transport: 'authenticated' }),
    pairing: pairing('paired'),
    desktopRuntimeReachable: true,
  });
  assert.equal(ready.id, CUSTOMER_STATES.FULL_READY);
  assert.equal(ready.primaryLabel, 'Open analysis');
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
