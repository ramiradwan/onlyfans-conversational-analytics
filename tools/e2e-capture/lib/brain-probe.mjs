import { randomUUID } from 'node:crypto';

import {
  DEV_ACCOUNT_ID,
  DEV_AUTH_TICKET,
} from '../../../extension/transport/agent-websocket.mjs';

const BRIDGE_URL = 'ws://localhost:8000/ws/bridge';

export function readBrainSummary({ timeoutMs = 10_000 } = {}) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(BRIDGE_URL);
    const bridgeSessionId = randomUUID();
    const helloId = randomUUID();
    let snapshot = null;
    let agent = null;
    let settled = false;
    const timeout = setTimeout(() => finish(new Error('Timed out reading Brain state.')), timeoutMs);

    function finish(error = null) {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      try {
        socket.close(1000, 'E2E probe complete');
      } catch {
        // The socket may not have opened.
      }
      if (error) reject(error);
      else resolve({
        viewRevision: snapshot.view_revision,
        conversationCount: snapshot.conversations.length,
        messageCount: snapshot.conversations.reduce(
          (total, conversation) => total + conversation.messages.length,
          0,
        ),
        agentStatus: agent.status,
        appliedConfigRevision: agent.applied_config_revision,
        requiredConfigRevision: agent.required_config_revision,
        lastHeartbeatAt: agent.last_heartbeat_at,
        connectionToken: agent.connection_id,
      });
    }

    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({
        type: 'bridge.hello',
        protocol_version: '1',
        message_id: helloId,
        payload: {
          auth_ticket: DEV_AUTH_TICKET,
          bridge_session_id: bridgeSessionId,
          requested_creator_account_id: DEV_ACCOUNT_ID,
          capabilities: ['state.snapshot', 'state.delta', 'presence.state'],
          client_version: 'e2e-capture-1',
          last_view_revision: null,
        },
      }));
    });
    socket.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        finish(new Error('Brain probe received malformed JSON.'));
        return;
      }
      if (message.type === 'protocol.error') {
        finish(new Error(`Brain probe protocol error: ${message.payload?.code ?? 'unknown'}`));
        return;
      }
      if (message.type === 'state.snapshot') snapshot = message.payload;
      if (message.type === 'agent.state') agent = message.payload;
      if (snapshot !== null && agent !== null) finish();
    });
    socket.addEventListener('error', () => finish(new Error('Brain probe WebSocket failed.')));
    socket.addEventListener('close', () => {
      if (!settled) finish(new Error('Brain probe closed before state was received.'));
    });
  });
}
