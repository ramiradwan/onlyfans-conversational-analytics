import { createReadOnlyAgentRuntime } from './transport/read-only-agent-runtime.mjs';
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';
import {
  createBrainBindingBridge,
  createChromeAdapter,
} from './transport/read-only-chrome-adapter.mjs';
import { CaptureDiagnostics } from './transport/read-only-capture-ingestion.mjs';
import { DeliveryCaptureIngestionService } from './transport/read-only-delivery-capture-ingestion.mjs';
import { createAccountBoundCaptureMessageBridge } from './transport/account-bound-capture-bridge.mjs';
import { createProvisioningIdentityBridge } from './transport/provisioning-identity.mjs';
import { createSecureLocalFetch } from './transport/secure-local-fetch.mjs';
import { LOCAL_SERVICE_HEALTH, LOCAL_SERVICE_ORIGIN } from './transport/local-service-endpoints.mjs';
import { ConsentController } from './runtime/consent-controller.mjs';
import { PreviewMetricsStore } from './runtime/preview-metrics.mjs';
import { clearExtensionLocalData } from './runtime/local-data.mjs';
import { OperationScope, SerialExecutor } from './runtime/operation-scope.mjs';
import { ActivationEvidenceStore } from './runtime/activation-evidence.mjs';
import { LegalActivationController } from './runtime/legal-activation-controller.mjs';
import { LegalConsentAuthorization } from './runtime/legal-consent-authorization.mjs';
import { legalReleaseBindings } from './runtime/legal-release-bindings.mjs';

let lastStartupErrorCode = null;
const secureLocalFetch = createSecureLocalFetch();
export const chromeAdapter = createChromeAdapter();
export const captureDiagnostics = new CaptureDiagnostics((diagnostic) => {
  console.warn('[Conversation Analytics] capture observation dropped', diagnostic);
});
export const agentRuntime = createReadOnlyAgentRuntime({
  chromeAdapter,
  chromeApi: chrome,
  signerFactory: (options) => createChromeBrowserSigningProvider(options),
  onStartupError: () => {
    lastStartupErrorCode = 'startup_failed';
    console.error('[Conversation Analytics] local Agent startup failed; a later consented wake will retry');
  },
});
export const captureScope = new OperationScope();
export const controlQueue = new SerialExecutor();

let consentController = null;
export const brainBindingBridge = createBrainBindingBridge({
  adapter: chromeAdapter,
  runtime: agentRuntime,
  onBound: () => consentController?.reconcile(),
  ensureReady: () => consentController.initialize(),
  allowsBinding: () => consentController?.state.mode === 'full'
    && ['identity', 'full'].includes(consentController.phase),
  runBindingOperation: (work) => consentController.runLegalOperation(work),
});
export const provisioningIdentityBridge = createProvisioningIdentityBridge({
  allowedOrigins: [LOCAL_SERVICE_ORIGIN],
  currentConsent: () => consentController?.state,
  ensureReady: () => consentController.initialize(),
  allowsIdentity: () => consentController?.state.mode === 'full'
    && ['identity', 'full'].includes(consentController.phase),
});
export const captureIngestion = new DeliveryCaptureIngestionService({
  runtime: agentRuntime,
  diagnostics: captureDiagnostics,
});
export const captureMessageBridge = createAccountBoundCaptureMessageBridge({
  ingestion: captureIngestion,
  runtime: agentRuntime,
  provisioningIdentityBridge,
  allowsCapture: () => consentController?.allowsFullCapture() === true,
  diagnostics: captureDiagnostics,
  operationScope: captureScope,
  currentConsent: () => consentController?.state,
  ensureReady: () => consentController.initialize(),
});

export const previewMetrics = new PreviewMetricsStore({ storage: chrome.storage.local });
export const activationEvidenceStore = new ActivationEvidenceStore({
  softwareVersion: chrome.runtime.getManifest().version,
});
export const legalConsentAuthorization = new LegalConsentAuthorization({
  evidenceStore: activationEvidenceStore,
  bindings: legalReleaseBindings,
  storage: chrome.storage.local,
});

let agentWorkerInstanceId = null;

function runtimeSummary() {
  const transport = agentRuntime.transport;
  const durableMeta = transport?.outbox?.meta ?? null;
  if (transport !== null && agentWorkerInstanceId === null) agentWorkerInstanceId = crypto.randomUUID();
  if (transport !== null) lastStartupErrorCode = null;
  return {
    runtime_ready: transport !== null,
    socket_open: transport?.socket?.readyState === WebSocket.OPEN,
    pending_entries: durableMeta?.outbox_count ?? 0,
    captured_chats: durableMeta?.entity_counts?.chats ?? 0,
    captured_messages: durableMeta?.entity_counts?.messages ?? 0,
    startup_error_code: lastStartupErrorCode,
    history_error_code: null,
    capture_drop_counts: captureDiagnostics.snapshot(),
    transport_state: transport?.session
      ? 'authenticated'
      : transport?.socket?.readyState === WebSocket.OPEN
        ? 'authenticating'
        : 'disconnected',
  };
}

consentController = new ConsentController({
  chromeApi: chrome,
  runtime: agentRuntime,
  adapter: chromeAdapter,
  brainBindingBridge,
  provisioningIdentityBridge,
  previewMetrics,
  clearLocalData: () => clearExtensionLocalData(),
  activeModeAuthorization: legalConsentAuthorization,
  captureScope,
  controlQueue,
  activationEvidenceStore,
  runtimeSummary,
  fetchImpl: (_url, init) => secureLocalFetch(LOCAL_SERVICE_HEALTH, init),
});
export { consentController };

export const legalActivationController = new LegalActivationController({
  chromeApi: chrome,
  consentController,
  evidenceStore: activationEvidenceStore,
  bindings: legalReleaseBindings,
});

export async function legalActivationAuditSnapshot() {
  return legalActivationController.exportAuditTrail();
}
Object.defineProperty(globalThis, '__OFCA_LEGAL_ACTIVATION_AUDIT__', {
  configurable: false,
  enumerable: false,
  value: legalActivationAuditSnapshot,
  writable: false,
});

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
    appliedConfigRevision: agentRuntime.configuration?.activeDocument?.config_revision ?? null,
    enabledResources: rules.filter((rule) => rule.enabled === true).map((rule) => rule.resource).sort(),
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

captureMessageBridge.register();
legalActivationController.register();
consentController.register();
void consentController.initialize().catch(() => undefined);
