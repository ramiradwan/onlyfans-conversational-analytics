import { safeCompanionCloseReason } from '../../../extension/transport/companion-channel.mjs';

const EVENTS = new Set(['connect-start', 'connect-admitted', 'circuit-reset', 'channel-close', 'invalidate']);
const ERROR_CLASSES = new Set(['Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'EvalError']);
const number = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const boolean = (value) => typeof value === 'boolean' ? value : null;

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
