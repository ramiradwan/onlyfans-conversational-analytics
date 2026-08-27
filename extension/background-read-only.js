import { createReadOnlyAgentRuntime } from './transport/read-only-agent-runtime.mjs';
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';
import {
  createBrainBindingBridge,
  createChromeAdapter,
} from './transport/read-only-chrome-adapter.mjs';
import {
  CaptureDiagnostics,
  CaptureIngestionService,
  createCaptureMessageBridge,
} from './transport/read-only-capture-ingestion.mjs';
import { createProvisioningIdentityBridge } from './transport/provisioning-identity.mjs';
import { ConsentController } from './runtime/consent-controller.mjs';
import { PreviewMetricsStore } from './runtime/preview-metrics.mjs';
import { clearExtensionLocalData } from './runtime/local-data.mjs';

export const chromeAdapter = createChromeAdapter();
export const agentRuntime = createReadOnlyAgentRuntime({
  chromeAdapter,
  signerFactory: (options) => createChromeBrowserSigningProvider(options),
  onStartupError: () => {
    // Keep diagnostics free of account data, credentials, and captured content.
    console.error('[Conversation Analytics] local Agent startup failed; a later consented wake will retry');
  },
});

let consentController = null;
export const brainBindingBridge = createBrainBindingBridge({
  adapter: chromeAdapter,
  runtime: agentRuntime,
  onBound: () => consentController?.reconcile(),
});
export const provisioningIdentityBridge = createProvisioningIdentityBridge();

export const captureDiagnostics = new CaptureDiagnostics((diagnostic) => {
  console.warn('[Conversation Analytics] capture observation dropped', diagnostic);
});
export const captureIngestion = new CaptureIngestionService({
  runtime: agentRuntime,
  diagnostics: captureDiagnostics,
});

const consentGatedIngestion = Object.freeze({
  rejectBridgeMessage: () => captureIngestion.rejectBridgeMessage(),
  async ingest(observation) {
    if (!consentController?.allowsFullCapture()) {
      captureDiagnostics.record('capture_disabled', observation?.event_type);
      return { ok: false, code: 'capture_disabled', retryable: false };
    }
    return captureIngestion.ingest(observation);
  },
});
export const captureMessageBridge = createCaptureMessageBridge({
  ingestion: consentGatedIngestion,
});

export const previewMetrics = new PreviewMetricsStore({
  storage: chrome.storage.local,
});
let agentWorkerInstanceId = null;

function runtimeSummary() {
  const transport = agentRuntime.transport;
  const durableMeta = transport?.outbox?.meta ?? null;
  if (transport !== null && agentWorkerInstanceId === null) {
    agentWorkerInstanceId = crypto.randomUUID();
  }
  return {
    runtime_ready: transport !== null,
    socket_open: transport?.socket?.readyState === WebSocket.OPEN,
    pending_entries: durableMeta?.outbox_count ?? 0,
    captured_chats: durableMeta?.entity_counts?.chats ?? 0,
    captured_messages: durableMeta?.entity_counts?.messages ?? 0,
  };
}

consentController = new ConsentController({
  runtime: agentRuntime,
  adapter: chromeAdapter,
  brainBindingBridge,
  provisioningIdentityBridge,
  previewMetrics,
  clearLocalData: () => clearExtensionLocalData(),
  runtimeSummary,
});
export { consentController };

/** Payload-free worker diagnostics used by local health checks and the system E2E harness. */
export async function agentDiagnosticSnapshot(alarmName = 'ofca-agent-reconcile') {
  const transport = agentRuntime.transport;
  const durableMeta = transport?.outbox?.meta ?? null;
  const rules = agentRuntime.configuration?.activeDocument?.capture_policy?.rules ?? [];
  const alarm = await chrome.alarms.get(alarmName);
  const consent = await consentController.status();
  return {
    workerInstanceId: agentWorkerInstanceId,
    consentMode: consent.consent.mode,
    capturePhase: consent.phase,
    runtimeReady: transport !== null,
    socketOpen: transport?.socket?.readyState === WebSocket.OPEN,
    sessionBound: transport?.session !== null && transport?.session !== undefined,
    heartbeatTimerPresent: transport?.heartbeatTimer !== null && transport?.heartbeatTimer !== undefined,
    syncRequired: transport?.syncRequired ?? null,
    appliedConfigRevision:
      agentRuntime.configuration?.activeDocument?.config_revision ?? null,
    enabledResources: rules
      .filter((rule) => rule.enabled === true)
      .map((rule) => rule.resource)
      .sort(),
    reconcileAlarm: alarm === undefined ? null : {
      name: alarm.name,
      scheduledTime: alarm.scheduledTime,
      periodInMinutes: alarm.periodInMinutes ?? null,
    },
    drops: captureDiagnostics.snapshot(),
    preview: consent.preview,
    outbox: durableMeta === null ? null : {
      lastSourceSeq: durableMeta.last_source_seq,
      acknowledgedSourceSeq: durableMeta.acknowledged_source_seq,
      pendingEntries: durableMeta.outbox_count,
      chatCount: durableMeta.entity_counts.chats,
      messageCount: durableMeta.entity_counts.messages,
      coverageEvidenceCount: durableMeta.entity_counts.coverage_evidence,
      pendingSnapshot: durableMeta.pending_snapshot !== null,
    },
  };
}

Object.defineProperty(globalThis, '__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__', {
  configurable: false,
  enumerable: false,
  value: agentDiagnosticSnapshot,
  writable: false,
});

// Listener registration is synchronous. No Agent identity, content script, or
// account runtime is created until the stored consent state has been checked.
captureMessageBridge.register();
consentController.register();
void consentController.initialize().catch(() => undefined);
