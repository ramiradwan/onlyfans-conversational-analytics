// This function is serialized into the browser and has no module dependencies.
export function probeBrowserState({ authTicket, creatorAccountId, timeout, catchup }) {
  return new Promise(resolve => {
    let socket, timer, snapshot = null, agent = null, settled = false;
    let opened = false, helloSent = false, transportError = false, pendingFailure = null;
    let closeCode = null, closeClean = null, protocolCode = null;
    const phase = () => !socket ? 'constructing' : !opened ? 'connecting' : !helloSent ? 'hello_not_sent'
      : snapshot === null && agent === null ? 'awaiting_snapshot_and_agent'
      : snapshot === null ? 'awaiting_snapshot' : agent === null ? 'awaiting_agent' : 'state_received';
    const finish = (code = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      for (const [name, listener] of listeners) socket?.removeEventListener(name, listener);
      try { socket?.close(1000, 'E2E probe complete'); } catch {}
      if (code === null) resolve({ ok: true, state: { snapshot, agent } });
      else resolve({ ok: false, observation: {
        code, phase: phase(), opened, hello_sent: helloSent, snapshot_seen: snapshot !== null,
        agent_seen: agent !== null, transport_error: transportError, close_code: closeCode,
        close_clean: closeClean, protocol_code: protocolCode, transport_detail: 'unavailable',
      } });
    };
    const fail = code => { if (pendingFailure === null) finish(code); };
    const onOpen = () => {
      opened = true;
      try {
        socket.send(JSON.stringify({
          type: 'bridge.hello', protocol_version: '2', message_id: helloId,
          payload: { auth_ticket: authTicket, bridge_session_id: bridgeSessionId,
            requested_creator_account_id: creatorAccountId,
            capabilities: ['state.snapshot', 'state.delta', 'presence.state', 'message.page',
              ...(catchup ? ['state.catchup_freshness'] : [])],
            client_version: 'e2e-capture-2', last_view_revision: null },
        }));
        helloSent = true;
      } catch { fail('send_failed'); }
    };
    const onMessage = event => {
      let message;
      try { message = JSON.parse(String(event.data)); }
      catch { fail('malformed_json'); return; }
      if (message === null) { fail('malformed_json'); return; }
      if (message?.type === 'protocol.error') {
        const code = message.payload?.code;
        protocolCode = ['unauthorized', 'identity_conflict', 'validation_failed', 'unsupported_version',
          'wrong_role', 'pre_handshake'].includes(code) ? code : 'unknown';
        fail('protocol_error'); return;
      }
      if (message?.type === 'state.snapshot') snapshot = message.payload;
      if (message?.type === 'agent.state') agent = message.payload;
      if (snapshot !== null && agent !== null && pendingFailure === null) finish();
    };
    const onError = () => {
      transportError = true;
      pendingFailure ??= 'socket_error';
      // A normal close may follow error; the original timer remains the only deadline.
    };
    const onClose = event => {
      closeCode = Number.isInteger(event.code) && event.code >= 0 && event.code <= 4999 ? event.code : null;
      closeClean = typeof event.wasClean === 'boolean' ? event.wasClean : null;
      finish(pendingFailure ?? 'socket_closed');
    };
    const listeners = [['open', onOpen], ['message', onMessage], ['error', onError], ['close', onClose]];
    let bridgeSessionId, helloId;
    try {
      socket = new WebSocket('ws://bridge.localhost:17871/ws/bridge');
      bridgeSessionId = crypto.randomUUID(); helloId = crypto.randomUUID();
      timer = setTimeout(() => finish(pendingFailure ?? 'deadline'), timeout);
      for (const [name, listener] of listeners) socket.addEventListener(name, listener);
    } catch { finish('construction_failed'); }
  });
}

const KEYS = ['code', 'phase', 'opened', 'hello_sent', 'snapshot_seen', 'agent_seen', 'transport_error',
  'close_code', 'close_clean', 'protocol_code', 'transport_detail'];
const BOOLEANS = ['opened', 'hello_sent', 'snapshot_seen', 'agent_seen', 'transport_error'];
const CODES = ['construction_failed', 'send_failed', 'malformed_json', 'protocol_error', 'socket_error', 'socket_closed', 'deadline'];
const PHASES = ['constructing', 'connecting', 'hello_not_sent', 'awaiting_snapshot_and_agent',
  'awaiting_snapshot', 'awaiting_agent', 'state_received'];

function unavailable() {
  return { code: 'evaluation_unavailable', phase: 'unavailable', opened: null, hello_sent: null,
    snapshot_seen: null, agent_seen: null, transport_error: null, close_code: null, close_clean: null,
    protocol_code: null, transport_detail: 'unavailable' };
}

function closedObservation(value) {
  if (!value || Object.keys(value).length !== KEYS.length || !KEYS.every(key => Object.hasOwn(value, key))
      || !CODES.includes(value.code) || !PHASES.includes(value.phase)
      || !BOOLEANS.every(key => typeof value[key] === 'boolean')
      || !(value.close_code === null || Number.isInteger(value.close_code) && value.close_code >= 0 && value.close_code <= 4999)
      || !(value.close_clean === null || typeof value.close_clean === 'boolean')
      || ![null, 'unauthorized', 'identity_conflict', 'validation_failed', 'unsupported_version',
        'wrong_role', 'pre_handshake', 'unknown'].includes(value.protocol_code)
      || value.transport_detail !== 'unavailable') return unavailable();
  return Object.fromEntries(KEYS.map(key => [key, value[key]]));
}

export async function readObservedBrainState(page, parameters) {
  let result;
  try { result = await page.evaluate(probeBrowserState, parameters); }
  catch { throw new Error(`Brain probe failed: ${JSON.stringify(unavailable())}`); }
  if (result?.ok === true) return result.state;
  throw new Error(`Brain probe failed: ${JSON.stringify(closedObservation(result?.observation))}`);
}
