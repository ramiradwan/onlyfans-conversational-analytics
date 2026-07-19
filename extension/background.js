import { createAgentRuntime } from './transport/agent-runtime.mjs';
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';
import {
  createBrainBindingBridge,
  createChromeAdapter,
} from './transport/chrome-adapter.mjs';
import {
  CaptureDiagnostics,
  CaptureIngestionService,
  createCaptureMessageBridge,
} from './transport/capture-ingestion.mjs';

export const chromeAdapter = createChromeAdapter();
export const agentRuntime = createAgentRuntime({
  chromeAdapter,
  signerFactory: (options) => createChromeBrowserSigningProvider(options),
  onStartupError: () => {
    // Keep diagnostics free of account data, credentials, and captured content.
    console.error('[Agent] startup failed; a later extension wake will retry');
  },
});
export const brainBindingBridge = createBrainBindingBridge({
  adapter: chromeAdapter,
  runtime: agentRuntime,
});

export const captureDiagnostics = new CaptureDiagnostics((diagnostic) => {
  console.warn('[Agent] capture observation dropped', diagnostic);
});
export const captureIngestion = new CaptureIngestionService({
  runtime: agentRuntime,
  diagnostics: captureDiagnostics,
});
export const captureMessageBridge = createCaptureMessageBridge({
  ingestion: captureIngestion,
});
const agentWorkerInstanceId = crypto.randomUUID();

/** Payload-free worker diagnostics used by local health checks and the system E2E harness. */
export async function agentDiagnosticSnapshot(alarmName = 'ofca-agent-reconcile') {
  const transport = agentRuntime.transport;
  const durableMeta = transport?.outbox?.meta ?? null;
  const rules = agentRuntime.configuration?.activeDocument?.capture_policy?.rules ?? [];
  const alarm = await chrome.alarms.get(alarmName);
  return {
    workerInstanceId: agentWorkerInstanceId,
    runtimeReady: transport !== null,
    socketOpen: transport?.socket?.readyState === WebSocket.OPEN,
    sessionBound: transport?.session !== null,
    heartbeatTimerPresent: transport?.heartbeatTimer !== null,
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

// Listener registration is synchronous. Durable initialization continues without
// top-level await so a failed storage/config step cannot leave the worker unwakeable.
captureMessageBridge.register();
brainBindingBridge.register();
agentRuntime.registerListeners();
void agentRuntime.wake().catch(() => undefined);
