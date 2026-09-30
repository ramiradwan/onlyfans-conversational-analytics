import { createSurfaceOpener, registerSurfaceNavigation } from './runtime/ui-surfaces.mjs';
import { registerDesktopPort } from './runtime/desktop-port.mjs';
import { probeDesktopRuntime } from './runtime/customer-journey.mjs';
import { applyControl, createSurfaceReporter } from './runtime/browser-surface.mjs';
import { createAgentRuntime } from './transport/agent-runtime.mjs';
import { createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';
import { AgentWebSocketClient } from './transport/agent-websocket.mjs';
import { createCompanionClient } from './runtime/companion-client.mjs';
import { accountDatabaseName } from './transport/indexeddb-ingestion-storage.mjs';
import { CaptureDiagnostics } from './transport/capture-ingestion.mjs';
import { DeliveryCaptureIngestionService } from './transport/delivery-capture-ingestion.mjs';
import { createAccountBoundCaptureMessageBridge } from './transport/account-bound-capture-bridge.mjs';
import { createProvisioningIdentityBridge } from './transport/provisioning-identity.mjs';
import { LOCAL_SERVICE_ORIGIN } from './transport/local-service-endpoints.mjs';
import { ConsentController } from './runtime/consent-controller.mjs';
import { PreviewMetricsStore } from './runtime/preview-metrics.mjs';
import { clearExtensionLocalData } from './runtime/local-data.mjs';
import { OperationScope, SerialExecutor } from './runtime/operation-scope.mjs';
import { ActivationEvidenceStore } from './runtime/activation-evidence.mjs';
import { LegalActivationController } from './runtime/legal-activation-controller.mjs';
import { LegalConsentAuthorization } from './runtime/legal-consent-authorization.mjs';
import { legalReleaseBindings } from './runtime/legal-release-bindings.mjs';

let lastStartupErrorCode = null;
// Delivery progress changes often; open pages re-read at most once a second.
let deliverySignalTimer = null;
const signalDeliveryProgress = () => {
  if (deliverySignalTimer !== null) return;
  deliverySignalTimer = setTimeout(() => { deliverySignalTimer = null; companionClient.notifySurfaces(); }, 1_000);
};
export const companionClient = createCompanionClient({
  accountDatabaseName,
  allowsFull: () => consentController?.state.mode === 'full',
  allowsControl: () => consentController?.state.mode === 'paused' && consentController.state.resume_mode === 'full',
  onControl: (action) => applyControl(consentController, action),
  detectedAccountId: () => provisioningIdentityBridge.currentAccountId(),
});
export const chromeAdapter = companionClient.adapter;
export const captureDiagnostics = new CaptureDiagnostics((diagnostic) => {
  console.warn('[Conversation Analytics] capture observation dropped', diagnostic);
});
export const agentRuntime = createAgentRuntime({
  chromeAdapter,
  chromeApi: chrome,
  configHttpFactory: () => companionClient.configAdapter,
  catchupRpc: (...args) => companionClient.configAdapter.catchupRpc(...args),
  captureState: () => consentController.captureState(),
  transportFactory: (options) => new AgentWebSocketClient({
    ...options,
    webSocketFactory: companionClient.webSocketFactory,
    onSession: (...values) => { options.onSession?.(...values); companionClient.notifySurfaces(); },
    onSessionLost: (...values) => { options.onSessionLost?.(...values); companionClient.notifySurfaces(); },
    onIngestAcknowledged: (...values) => { options.onIngestAcknowledged?.(...values); signalDeliveryProgress(); },
    onIngestRejected: (...values) => { options.onIngestRejected?.(...values); signalDeliveryProgress(); },
  }),
  signerFactory: (options) => createChromeBrowserSigningProvider(options),
  onStartupError: () => {
    lastStartupErrorCode = 'startup_failed';
    console.error('[Conversation Analytics] local Agent startup failed; a later consented wake will retry');
  },
});
export const captureScope = new OperationScope();
export const controlQueue = new SerialExecutor();

let consentController = null;
export const provisioningIdentityBridge = createProvisioningIdentityBridge({
  allowedOrigins: [LOCAL_SERVICE_ORIGIN],
  currentConsent: () => consentController?.state,
  ensureReady: () => consentController.initialize(),
  allowsIdentity: () => consentController?.state.mode === 'full'
    && ['identity', 'full'].includes(consentController.phase),
  allowsExternalIdentity: async () => {
    if (consentController?.state.mode !== 'full' || consentController.phase !== 'identity') return false;
    // External queries already hold the identity queue. Full UI status also
    // reads that queue, so admission must inspect only the persisted pairing.
    const paired = await companionClient.hasSavedPairing();
    return !paired && consentController.state.mode === 'full' && consentController.phase === 'identity';
  },
});
provisioningIdentityBridge.onAccountChange(() => companionClient.invalidate());
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
    history_error_code: agentRuntime.history?.historyErrorCode?.() ?? null,
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
  provisioningIdentityBridge,
  previewMetrics,
  clearLocalData: () => clearExtensionLocalData(),
  activeModeAuthorization: legalConsentAuthorization,
  captureScope,
  controlQueue,
  activationEvidenceStore,
  runtimeSummary,
  hasSavedPairing: () => companionClient.hasSavedPairing(),
  fetchImpl: async () => ({ ok: companionClient.connected }),
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
  const recovery = (await chrome.storage.local.get(['companion_recovery_v1'])).companion_recovery_v1;
  return {
    capturedAt: Date.now(),
    workerInstanceId: agentWorkerInstanceId,
    consentMode: consent.consent.mode,
    capturePhase: consent.phase,
    runtimeReady: transport !== null,
    transportStopped: transport?.stopped ?? null,
    reconnectAllowed: transport?.reconnectAllowed ?? null,
    socketOpen: transport?.socket?.readyState === WebSocket.OPEN,
    sessionBound: transport?.session !== null && transport?.session !== undefined,
    heartbeatTimerPresent: transport?.heartbeatTimer !== null && transport?.heartbeatTimer !== undefined,
    lastHeartbeatSentAt: transport?.lastHeartbeatSentAt === null || transport?.lastHeartbeatSentAt === undefined
      ? null : Math.round(performance.timeOrigin + transport.lastHeartbeatSentAt),
    reconnectTimerPresent: transport?.reconnectTimer !== null && transport?.reconnectTimer !== undefined,
    connectionEvents: companionClient.diagnosticEvents,
    recoveryAttempts: recovery?.attempts ?? null,
    recoveryNextAttemptInMs: recovery === undefined ? null
      : Math.max(0, recovery.next_attempt_at - Date.now()),
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
companionClient.registerPopup({
  onPaired: () => consentController.reconcile(),
  onForget: () => consentController.reconcile(),
});
companionClient.onRevoked(() => consentController.reconcile());
void consentController.initialize().catch(() => undefined);

const openSurface = createSurfaceOpener(chrome);
registerSurfaceNavigation(chrome, openSurface);

// The same change events feed open pages, the desktop port, and the browser
// state that Brain shows in the desktop app.
const surfaceReporter = createSurfaceReporter({
  companion: companionClient,
  relevant: () => consentController?.state.mode === 'full'
    || (consentController?.state.mode === 'paused' && consentController.state.resume_mode === 'full'),
  readState: async () => ({
    consent: await consentController.status(),
    legal: await legalActivationController.status(),
  }),
});
const signalSurfaces = () => { companionClient.notifySurfaces(); surfaceReporter.changed(); };
chrome.storage.onChanged.addListener((_changes, area) => { if (area === 'local') surfaceReporter.changed(); });
void consentController.initialize().then(() => surfaceReporter.changed(), () => undefined);
chrome.permissions?.onAdded?.addListener(signalSurfaces);
chrome.permissions?.onRemoved?.addListener(signalSurfaces);
chrome.tabs?.onUpdated?.addListener((_tabId, changeInfo) => {
  if (changeInfo?.status !== 'complete') return;
  signalSurfaces();
  // Chromium can publish "complete" just before the newly injected content
  // bridge answers status. One bounded follow-up keeps event-driven surfaces
  // accurate without restoring periodic polling.
  setTimeout(signalSurfaces, 500);
});
provisioningIdentityBridge.onAccountChange(signalSurfaces);

export const desktopPort = registerDesktopPort({
  chromeApi: chrome,
  companion: companionClient,
  readState: async () => ({
    consent: await consentController.status(),
    legal: await legalActivationController.status(),
    pairing: await companionClient.status(),
  }),
  // Each step opens the one extension page that owns it. Site access and the
  // history permission need a click there because Chrome requires the gesture.
  openStep: (step, { anchorTab }) => openSurface(['setup', 'access'].includes(step)
    ? { surface: 'setup', section: 'desktop', presentation: 'window', anchorTab }
    : { surface: 'options', section: step === 'history' ? 'history' : 'connection', presentation: 'window', anchorTab }),
  onPaired: () => consentController.reconcile(),
  changeSources: [
    (changed) => {
      const listener = (_changes, area) => { if (area === 'local') changed(); };
      chrome.storage.onChanged.addListener(listener);
      return () => chrome.storage.onChanged.removeListener(listener);
    },
    (changed) => {
      const listener = (_tabId, changeInfo) => { if (changeInfo?.status === 'complete') changed(); };
      chrome.tabs?.onUpdated?.addListener(listener);
      return () => chrome.tabs?.onUpdated?.removeListener?.(listener);
    },
  ],
});
desktopPort.register();

// Installed after the desktop app: open setup on the desktop-guided path. It
// still asks for every choice; it only skips the Preview-first framing.
chrome.runtime.onInstalled?.addListener(({ reason } = {}) => {
  if (reason !== 'install') return;
  void probeDesktopRuntime().then((present) => {
    if (present) return openSurface({ surface: 'setup', section: 'desktop' });
    return undefined;
  }).catch(() => undefined);
});
