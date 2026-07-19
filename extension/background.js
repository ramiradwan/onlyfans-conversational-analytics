import { createAgentRuntime } from './transport/agent-runtime.mjs';
import {
  CaptureDiagnostics,
  CaptureIngestionService,
  createCaptureMessageBridge,
} from './transport/capture-ingestion.mjs';

export const agentRuntime = createAgentRuntime({
  onStartupError: () => {
    // Keep diagnostics free of account data, credentials, and captured content.
    console.error('[Agent] startup failed; a later extension wake will retry');
  },
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
  const cache = transport?.outbox?.cache;
  const rules = agentRuntime.configuration?.activeDocument?.capture_policy?.rules ?? [];
  const alarm = await chrome.alarms.get(alarmName);
  const pendingEntries = cache === null || cache === undefined
    ? []
    : [...cache.outbox.values()].sort((left, right) => left.source_seq - right.source_seq);
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
    outbox: cache === null || cache === undefined ? null : {
      lastSourceSeq: cache.meta.last_source_seq,
      acknowledgedSourceSeq: cache.meta.acknowledged_source_seq,
      pendingEntries: cache.outbox.size,
      pendingSequences: pendingEntries.map((entry) => entry.source_seq),
      pendingEventIds: pendingEntries.map((entry) => entry.event_id),
      chatCount: cache.chats.size,
      messageCount: cache.messages.size,
      pendingSnapshot: cache.pendingSnapshot !== null,
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
agentRuntime.registerListeners();
void agentRuntime.wake().catch(() => undefined);
