import { randomUUID } from 'node:crypto';
import http from 'node:http';

import { BRAIN_ORIGIN, BRAIN_PORT } from './brain.mjs';

const CONFIG_PATTERN = /<script[^>]*id=["']fastapi-config["'][^>]*>([\s\S]*?)<\/script>/i;
const CSRF_PATTERN = /<meta[^>]*name=["']csrf-token["'][^>]*content=["']([^"']+)["'][^>]*>/i;

function requestBrain(pathname, { headers = {}, method = 'GET' } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      headers: {
        Host: new URL(BRAIN_ORIGIN).host,
        ...headers,
      },
      host: '127.0.0.1',
      method,
      path: pathname,
      port: BRAIN_PORT,
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({
        body: Buffer.concat(chunks).toString('utf8'),
        status: response.statusCode ?? 0,
      }));
    });
    request.on('error', reject);
    request.end();
  });
}

async function servedBootstrap() {
  const response = await requestBrain('/', {
    headers: {
      Accept: 'text/html',
    },
  });
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Brain runtime bootstrap failed (${response.status}).`);
  }
  const html = response.body;
  const configMatch = CONFIG_PATTERN.exec(html);
  const csrfMatch = CSRF_PATTERN.exec(html);
  if (configMatch === null || csrfMatch === null) {
    throw new Error('Brain did not serve runtime configuration and CSRF metadata.');
  }
  let config;
  try {
    config = JSON.parse(configMatch[1]);
  } catch {
    throw new Error('Brain served malformed runtime configuration.');
  }
  if (
    typeof config !== 'object'
    || config === null
    || config.CREATOR_ID === undefined
    || typeof config.EXTENSION_ID !== 'string'
    || !/^[a-p]{32}$/.test(config.EXTENSION_ID)
    || typeof config.BRIDGE_AUTH_TICKET !== 'string'
    || config.BRIDGE_AUTH_TICKET.length === 0
    || typeof config.FASTAPI_WS_URL !== 'string'
    || config.FASTAPI_WS_URL.length === 0
    || config.API_BASE_URL !== BRAIN_ORIGIN
    || config.FASTAPI_WS_URL !== BRAIN_ORIGIN.replace('http://', 'ws://') + '/ws/bridge'
  ) {
    throw new Error('Brain runtime configuration is incomplete.');
  }
  return { config, csrfToken: csrfMatch[1] };
}

export async function readServedRuntimeConfig() {
  return (await servedBootstrap()).config;
}

export async function requestAgentPairingTicket() {
  const { config, csrfToken } = await servedBootstrap();
  const response = await requestBrain('/api/v1/agent/pairing', {
    headers: {
      Accept: 'application/json',
      Origin: BRAIN_ORIGIN,
      'X-CSRF-Token': csrfToken,
    },
    method: 'POST',
  });
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Agent pairing request failed (${response.status}).`);
  }
  const document = JSON.parse(response.body);
  if (
    typeof document?.pairing_ticket !== 'string'
    || document.pairing_ticket.length === 0
    || typeof document.expires_at !== 'string'
  ) {
    throw new Error('Brain returned an invalid Agent pairing response.');
  }
  return {
    creatorAccountId: config.CREATOR_ID,
    extensionId: config.EXTENSION_ID,
    pairingTicket: document.pairing_ticket,
    expiresAt: document.expires_at,
  };
}

export async function readBrainSummary({ timeoutMs = 10_000 } = {}) {
  const config = await readServedRuntimeConfig();
  return new Promise((resolve, reject) => {
    const probeUrl = new URL(config.FASTAPI_WS_URL);
    probeUrl.hostname = '127.0.0.1';
    const socket = new WebSocket(probeUrl);
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
      if (error) {
        reject(error);
        return;
      }
      const totalMessages = snapshot.analytics.total_messages;
      resolve({
        viewRevision: snapshot.view_revision,
        conversationCount: snapshot.conversations.length,
        messageCount: totalMessages.value,
        analyticsBasis: totalMessages.basis,
        analytics: snapshot.analytics,
        coverage: snapshot.coverage,
        projection: snapshot.projection,
        liveFreshness: snapshot.live_freshness,
        summaryOnly: snapshot.conversations.every((conversation) => (
          !Object.hasOwn(conversation, 'messages')
        )),
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
        protocol_version: '2',
        message_id: helloId,
        payload: {
          auth_ticket: config.BRIDGE_AUTH_TICKET,
          bridge_session_id: bridgeSessionId,
          requested_creator_account_id: config.CREATOR_ID,
          capabilities: ['state.snapshot', 'state.delta', 'presence.state', 'message.page'],
          client_version: 'e2e-capture-2',
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
