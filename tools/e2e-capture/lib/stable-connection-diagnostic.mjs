import { stripVTControlCharacters } from 'node:util';
import { safeCompanionCloseReason, safeCompanionChannelDiagnostic } from '../../../extension/transport/companion-channel.mjs';

const EVENTS = new Set(['connect-start', 'connect-admitted', 'connect-failed', 'circuit-reset', 'channel-close', 'facade-close', 'invalidate', 'credential-rotation-failed']);
const ERROR_CLASSES = new Set(['Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'EvalError']);
const number = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const boolean = (value) => typeof value === 'boolean' ? value : null;
const enumValue = (value, allowed) => value == null ? null : allowed.includes(value) ? value : 'other';

function rotationDiagnostic(value) {
  const failure = value?.failure;
  return {
    attempts: number(value?.attempts), completed: number(value?.completed),
    failure: failure == null ? null : {
      phase: enumValue(failure.phase, ['binding', 'rpc', 'response', 'commit']),
      cause: enumValue(failure.cause, ['binding_unavailable', 'channel_closed', 'account_mismatch',
        'rotation_response_invalid', 'binding_changed', 'session_request_refused', 'companion_session_refused',
        'companion_recovery_backoff', 'unknown_method']),
      errorName: enumValue(failure.errorName, ['Error', 'TypeError', 'CompanionChannelError', 'AbortError',
        'TimeoutError', 'InvalidStateError', 'QuotaExceededError', 'TransactionInactiveError']),
      signalAborted: boolean(failure.signalAborted), channelClosed: boolean(failure.channelClosed),
      bindingCurrent: boolean(failure.bindingCurrent),
      channel: safeCompanionChannelDiagnostic(failure.channel),
    },
  };
}

function configurationDiagnostic(value) {
  return {
    ...Object.fromEntries(['documentPresent', 'bundled', 'applied', 'required', 'revisionsMatch',
      'authorized', 'refreshPending', 'retryScheduled'].map(key => [key, boolean(value?.[key])])),
    retryAttempt: number(value?.retryAttempt),
    failureCode: enumValue(value?.failureCode, ['fetch_failed', 'persistence_failed', 'missing_persisted_config',
      'missing_session_authorization', 'session_authorization_changed', 'session_request_refused',
      'companion_session_refused', 'unauthorized', 'server_error', 'invalid_304', 'unexpected_status',
      'unsupported_schema', 'account_mismatch', 'revision_mismatch', 'signaled_digest_mismatch', 'etag_mismatch',
      'digest_mismatch', 'unsafe_capture_pattern', 'history_authorization_missing', 'unsafe_capture_policy',
      'unsupported_capability']),
  };
}

export function readSessionFailures(output) {
  const results = [];
  for (const line of String(output).split('\n')) {
    if (!line.startsWith('e2e-session-failure: ') || line.length > 16_384) continue;
    let value;
    try { value = JSON.parse(line.slice('e2e-session-failure: '.length)); } catch { continue; }
    results.push({
      method: enumValue(value?.method, ['agent.storage.rotate', 'agent.storage.unseal', 'agent.config.get',
        'capture.state.report', 'history.check.begin', 'agent.challenge', 'agent.authenticate', 'agent.analysis.readiness', 'session.serve']),
      causes: Array.isArray(value?.causes) ? value.causes.slice(0, 4).map(cause => ({
        errorName: enumValue(cause?.errorName, ['CompanionSessionError', 'CompanionRecordError', 'AuthenticationStateError',
          'CompanionPairingPersistenceError', 'LocalDataKeyError', 'RuntimeError', 'ValueError', 'TypeError', 'KeyError',
          'OperationalError', 'DatabaseError', 'IntegrityError', 'TimeoutError', 'ValidationError', 'PermissionError',
          'InvalidToken', 'InvalidTag', 'OSError', 'QueueFull', 'WebSocketDisconnect']),
        phase: enumValue(cause?.phase, ['dispatch', 'validate_config', 'open_bootstrap', 'seal_bootstrap',
          'policy', 'consume_config', 'ticket_binding', 'data_key', 'serve', 'receive', 'send', 'agent_socket', 'broadcast_catchup']),
        frames: Array.isArray(cause?.frames) ? cause.frames.slice(-8).map(frame => ({
          module: enumValue(frame?.module, ['rpc', 'authority', 'auth_store', 'pairing_store', 'bootstrap',
            'data_key', 'catchup', 'manager', 'channel', 'agent_socket']), line: number(frame?.line),
        })) : [],
      })) : [],
    });
    if (results.length > 24) results.shift();
  }
  return results;
}

export function createSessionFailureCollector() {
  const reports = [];
  let pending = '', discarding = false;
  return {
    append(chunk) {
      for (const part of String(chunk).split(/(?<=\n)/)) {
        if (!discarding) {
          pending += part;
          if (pending.length > 16_384) { pending = ''; discarding = true; }
        }
        if (part.endsWith('\n')) {
          if (!discarding) reports.push(...readSessionFailures(pending));
          if (reports.length > 24) reports.splice(0, reports.length - 24);
          pending = ''; discarding = false;
        }
      }
    },
    snapshot() { return structuredClone(reports); },
  };
}

export function buildWorkerRecoveryDiagnostic(extension) {
  const state = extension ?? {};
  const alarmTime = number(state.reconcileAlarm?.scheduledTime);
  const capturedAt = number(state.capturedAt);
  return {
    runtimeReady: boolean(state.runtimeReady),
    transportStopped: boolean(state.transportStopped),
    reconnectAllowed: boolean(state.reconnectAllowed),
    socketOpen: boolean(state.socketOpen),
    sessionBound: boolean(state.sessionBound),
    reconnectTimerPresent: boolean(state.reconnectTimerPresent),
    recoveryAttempts: number(state.recoveryAttempts),
    recoveryNextAttemptInMs: number(state.recoveryNextAttemptInMs),
    alarmPresent: state.reconcileAlarm != null,
    alarmDueInMs: alarmTime === null || capturedAt === null ? null : Math.max(0, alarmTime - capturedAt),
    acknowledgedSourceSeq: number(state.outbox?.acknowledgedSourceSeq),
    pendingEntries: number(state.outbox?.pendingEntries),
    credentialRotation: rotationDiagnostic(state.credentialRotation),
    configuration: configurationDiagnostic(state.configuration),
    connectionEvents: Array.isArray(state.connectionEvents) ? state.connectionEvents.slice(-24).map((entry) => ({
      event: EVENTS.has(entry.event) ? entry.event : 'other',
      code: Number.isInteger(entry.code) && entry.code >= 1000 && entry.code <= 4999 ? entry.code : null,
      reason: safeCompanionCloseReason(entry.reason),
      wasStable: boolean(entry.wasStable),
      channel: safeCompanionChannelDiagnostic(entry.channel),
    })) : [],
  };
}

export function logWorkerRecoveryCheckpoint(stage, state, failures = [], log = line => console.error(line)) {
  if (!['replay', 'replacement', 'alarm'].includes(stage)) return;
  try {
    const brainSessionFailures = readSessionFailures(failures.slice(-24)
      .map(value => 'e2e-session-failure: ' + JSON.stringify(value)).join('\n'));
    log('worker-recovery-checkpoint: ' + JSON.stringify({ stage,
      recovery: buildWorkerRecoveryDiagnostic(state), brainSessionFailures }));
  } catch {}
}

export function withoutReportedExpectStep(assertion, { expectConfig, setExpectConfig }) {
  const previous = expectConfig();
  setExpectConfig({ ...previous, testInfo: null });
  try { return assertion(); }
  finally { setExpectConfig(previous); }
}

export function redactStableConnectionAssertionError(error, actual, expected) {
  if (!(error instanceof Error)) return error;
  const tokens = [...new Set([actual, expected].filter((value) => typeof value === 'string' && value.length))]
    .sort((left, right) => right.length - left.length);
  const redact = (value) => {
    if (typeof value !== 'string') return value;
    // Matcher diff colors can split a token. The terminal reporter removes those
    // controls and rejoins it, so inspect the displayed text before replacement.
    // Keep formatting byte-for-byte when the field contains no sensitive token.
    const displayed = stripVTControlCharacters(value);
    const text = tokens.some(token => displayed.includes(token)) ? displayed : value;
    return tokens.reduce((result, token) => result.split(token).join('[redacted]'), text);
  };
  const visit = (failure) => {
    failure.message = redact(failure.message);
    failure.stack = redact(failure.stack);
    if (failure.matcherResult) {
      for (const [key, value] of Object.entries(failure.matcherResult)) {
        failure.matcherResult[key] = key === 'actual' || key === 'expected' ? undefined : redact(value);
      }
    }
    if (failure.cause instanceof Error) visit(failure.cause);
    if (Array.isArray(failure.errors)) {
      for (const child of failure.errors) if (child instanceof Error) visit(child);
    }
  };
  visit(error);
  return error;
}

export function buildStableConnectionDiagnostic({
  at, statusPolls, lastStatusPollAt, popupClosedAt, workerStartsDuringStep,
  originalWorkerAlive, brain, extension, stateError,
}) {
  return {
    at: number(at), statusPolls: number(statusPolls),
    lastStatusPollAt: number(lastStatusPollAt), popupClosedAt: number(popupClosedAt),
    workerStartsDuringStep: number(workerStartsDuringStep),
    workerChangedDuringStep: originalWorkerAlive === false,
    brain: brain === null ? null : {
      connectionPresent: boolean(brain.connectionPresent),
      lastHeartbeatAt: number(Date.parse(brain.lastHeartbeatAt)),
    },
    extension: extension === null ? null : {
      capturedAt: number(extension.capturedAt),
      runtimeReady: boolean(extension.runtimeReady),
      socketOpen: boolean(extension.socketOpen),
      sessionBound: boolean(extension.sessionBound),
      heartbeatTimerPresent: boolean(extension.heartbeatTimerPresent),
      lastHeartbeatSentAt: number(extension.lastHeartbeatSentAt),
      reconnectTimerPresent: boolean(extension.reconnectTimerPresent),
      recoveryAttempts: number(extension.recoveryAttempts),
      recoveryNextAttemptInMs: number(extension.recoveryNextAttemptInMs),
      connectionEvents: Array.isArray(extension.connectionEvents)
        ? extension.connectionEvents.slice(-24).map((entry) => ({
          at: number(entry.at),
          event: EVENTS.has(entry.event) ? entry.event : 'other',
          code: number(entry.code),
          reason: safeCompanionCloseReason(entry.reason),
          wasStable: boolean(entry.wasStable),
        })) : [],
    },
    stateError: stateError === null ? null : ERROR_CLASSES.has(stateError?.name) ? stateError.name : 'other',
  };
}
