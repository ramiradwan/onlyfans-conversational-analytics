import { BRAIN_ORIGIN } from './brain.mjs';
import { readServedRuntimeConfig } from './brain-probe-base.mjs';
import { readObservedBrainState } from './brain-probe-observation.mjs';

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
    ...(snapshot.catchup_freshness ? { catchupFreshness: snapshot.catchup_freshness } : {}),
    summaryOnly: snapshot.conversations.every((conversation) => (
      !Object.hasOwn(conversation, 'messages')
    )),
    agentStatus: agent.status,
    appliedConfigRevision: agent.applied_config_revision,
    requiredConfigRevision: agent.required_config_revision,
    lastHeartbeatAt: agent.last_heartbeat_at,
    connectionToken: agent.connection_id,
    browser: agent.browser ?? null,
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

export async function readBrainSummary(context, { timeoutMs = 10_000, catchup = false } = {}) {
  const config = await readServedRuntimeConfig(context);
  // Keep one already-loaded Bridge-origin page per browser context. Creating or
  // navigating a tab emits chrome.tabs.onUpdated, which is itself a production
  // Agent wake source; reusing this page lets hard-expiry tests observe Brain
  // without accidentally recreating the MV3 worker they just terminated.
  const page = await summaryProbePage(context, timeoutMs);
  const state = await readObservedBrainState(page, {
    authTicket: config.BRIDGE_AUTH_TICKET,
    creatorAccountId: config.CREATOR_ID,
    timeout: timeoutMs,
    catchup,
  });
  return normalizeSummary(state.snapshot, state.agent);
}
