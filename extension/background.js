import {
  AgentWebSocketClient,
  DEV_ACCOUNT_ID,
  DEV_AUTH_TICKET,
} from './transport/agent-websocket.mjs';
import {
  AgentConfigClient,
  AtomicConfigActivator,
} from './transport/agent-config-client.mjs';
import { createChromeAdapter } from './transport/chrome-adapter.mjs';
import { createConfigHttpAdapter } from './transport/config-http-adapter.mjs';
import { DurableIngestOutbox } from './transport/durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from './transport/indexeddb-ingestion-storage.mjs';

const chromeAdapter = createChromeAdapter();
const identity = await chromeAdapter.loadAgentIdentity();
const durableOutbox = new DurableIngestOutbox({
  storage: createIndexedDbIngestionStorage(),
  legacyStorage: chromeAdapter,
});
const ingestionState = await durableOutbox.initialize();
identity.lastAcknowledgedSourceSeq = Math.max(
  identity.lastAcknowledgedSourceSeq,
  ingestionState.acknowledged_source_seq,
);

const configActivator = new AtomicConfigActivator();
let agentTransport;
export const agentConfiguration = new AgentConfigClient({
  identity,
  creatorAccountId: DEV_ACCOUNT_ID,
  authTicket: DEV_AUTH_TICKET,
  http: createConfigHttpAdapter(),
  persistence: chromeAdapter,
  activator: configActivator,
  reportApplied: (report) => agentTransport?.sendConfigApplied(report) ?? false,
  onUnauthorized: () => agentTransport?.stop(),
});
await agentConfiguration.initialize();

agentTransport = new AgentWebSocketClient({
  identity,
  persistence: chromeAdapter,
  outbox: durableOutbox,
  configClient: agentConfiguration,
  health: () => agentConfiguration.healthSummary(),
});

chromeAdapter.onWake(() => agentTransport.ensureConnected());
agentTransport.start();

export { agentTransport };
