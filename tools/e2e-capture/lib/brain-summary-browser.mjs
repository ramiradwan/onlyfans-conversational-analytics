import { BRAIN_ORIGIN } from './brain.mjs';
import { readServedRuntimeConfig } from './brain-probe-base.mjs';

const summaryProbePages = new WeakMap();

function normalizeSummary(snapshot, agent) {
  const totalMessages = snapshot.analytics.total_messages;
  return {
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
  };
}

async function summaryProbePage(context, timeoutMs) {
  const existing = summaryProbePages.get(context);
  if (existing && !existing.isClosed()) return existing;

  const page = await context.newPage();
  try {
    await page.goto(`${BRAIN_ORIGIN}/health`, {
      waitUntil: 'domcontentloaded',
      timeout: timeoutMs,
    });
  } catch (error) {
    await page.close().catch(() => undefined);
    throw error;
  }
  summaryProbePages.set(context, page);
  page.on('close', () => {
    if (summaryProbePages.get(context) === page) summaryProbePages.delete(context);
  });
  return page;
}

export async function readBrainSummary(context, { timeoutMs = 10_000 } = {}) {
  const config = await readServedRuntimeConfig(context);
  // Keep one already-loaded Bridge-origin page per browser context. Creating or
  // navigating a tab emits chrome.tabs.onUpdated, which is itself a production
  // Agent wake source; reusing this page lets hard-expiry tests observe Brain
  // without accidentally recreating the MV3 worker they just terminated.
  const page = await summaryProbePage(context, timeoutMs);
  const state = await page.evaluate(({ authTicket, creatorAccountId, timeout }) => (
    new Promise((resolve, reject) => {
      const socket = new WebSocket('ws://bridge.localhost:17871/ws/bridge');
      const bridgeSessionId = crypto.randomUUID();
      const helloId = crypto.randomUUID();
      let snapshot = null;
      let agent = null;
      let settled = false;
      const timer = setTimeout(() => finish(new Error('Timed out reading Brain state.')), timeout);

      const finish = (error = null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { socket.close(1000, 'E2E probe complete'); } catch {}
        if (error !== null) reject(error);
        else resolve({ snapshot, agent });
      };

      socket.addEventListener('open', () => {
        socket.send(JSON.stringify({
          type: 'bridge.hello',
          protocol_version: '2',
          message_id: helloId,
          payload: {
            auth_ticket: authTicket,
            bridge_session_id: bridgeSessionId,
            requested_creator_account_id: creatorAccountId,
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
      socket.addEventListener('close', (event) => {
        if (!settled) {
          finish(new Error(
            `Brain probe closed before state was received (${event.code} ${event.reason || 'no-reason'}).`,
          ));
        }
      });
    })
  ), {
    authTicket: config.BRIDGE_AUTH_TICKET,
    creatorAccountId: config.CREATOR_ID,
    timeout: timeoutMs,
  });
  return normalizeSummary(state.snapshot, state.agent);
}
