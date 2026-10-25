import { StringDecoder } from 'node:string_decoder';

export const STARTUP_SCHEMA = 'browser-startup-diagnostic/v1';
const MAX_MS = 3_600_000;
const MAX_BYTES = 65_536;
const PHASES = {
  brain: ['entry_started', 'uvicorn_imported', 'endpoints_imported', 'main_imported', 'helpers_installed', 'uvicorn_entered'],
  native_permission: ['started', 'window_found', 'prompt_found', 'invoke_attempted', 'allow_returned', 'prompt_dismissed'],
};
const HEALTH = ['not_attempted', 'ready', 'non_2xx', 'connection_refused', 'connection_error', 'timeout'];
const ERRORS = ['aborted', 'not_found', 'access_denied', 'output_limit', 'other'];
const SIGNALS = ['SIGTERM', 'SIGKILL', 'other'];
const KEYS = ['schema', 'owner', 'reason', 'elapsed_ms', 'pid', 'spawn_ms', 'first_stdout_ms', 'first_stderr_ms',
  'stdout_bytes', 'stderr_bytes', 'exit_ms', 'close_ms', 'exit_code', 'close_exit_code', 'signal', 'close_signal',
  'error_kind', 'phases', 'phase_status', 'health_attempts', 'health_outcome'];
const exact = (v, keys) => v && !Array.isArray(v) && typeof v === 'object'
  && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
const integer = (v, max) => Number.isSafeInteger(v) && v >= 0 && v <= max;
const optional = (v, check) => v === null || check(v);

export function validStartupDiagnostic(v) {
  if (!exact(v, KEYS) || v.schema !== STARTUP_SCHEMA || !Object.hasOwn(PHASES, v.owner)
      || !(v.owner === 'brain' ? ['unhealthy', 'startup_exit'] : ['interaction_failed']).includes(v.reason)
      || !integer(v.elapsed_ms, MAX_MS) || !optional(v.pid, n => integer(n, 2 ** 32 - 1) && n > 0)
      || !['spawn_ms', 'first_stdout_ms', 'first_stderr_ms', 'exit_ms', 'close_ms'].every(k => optional(v[k], n => integer(n, v.elapsed_ms)))
      || !['stdout_bytes', 'stderr_bytes'].every(k => integer(v[k], MAX_BYTES))
      || !['exit_code', 'close_exit_code'].every(k => optional(v[k], n => Number.isSafeInteger(n) && Math.abs(n) <= 2 ** 32 - 1))
      || !['signal', 'close_signal'].every(k => optional(v[k], n => SIGNALS.includes(n)))
      || !optional(v.error_kind, n => ERRORS.includes(n))
      || !['empty', 'observed', 'limited'].includes(v.phase_status)
      || !integer(v.health_attempts, 10_000) || !HEALTH.includes(v.health_outcome)
      || !Array.isArray(v.phases) || v.phases.length > 6) return false;
  const seen = new Set(); let previous = 0;
  for (const row of v.phases) {
    if (!exact(row, ['phase', 'elapsed_ms']) || !PHASES[v.owner].includes(row.phase) || seen.has(row.phase)
        || !integer(row.elapsed_ms, v.elapsed_ms) || row.elapsed_ms < previous) return false;
    seen.add(row.phase); previous = row.elapsed_ms;
  }
  return (v.phase_status !== 'empty' || v.phases.length === 0)
    && (v.phase_status !== 'observed' || v.phases.length > 0)
    && (v.owner !== 'native_permission' || v.health_attempts === 0 && v.health_outcome === 'not_attempted');
}

function validNativeEnvelope(v) {
  const flags = ['window_found', 'prompt_found', 'invoke_attempted', 'allow_invoked', 'prompt_dismissed'];
  if (!exact(v, ['schema', 'request_kind', 'stage', ...flags, 'exit_code', 'signal', 'killed', 'operation_error_kind',
    'trace_source', 'trace_status', 'trace_incomplete', 'trace_bytes', 'child', 'timeout_fired', 'permission_state', 'startup'])
      || v.schema !== 'native-permission-diagnostic/v1'
      || !['preview', 'full', 'history', 'unspecified'].includes(v.request_kind)
      || !['unknown', ...PHASES.native_permission].includes(v.stage)
      || ![...flags, 'killed', 'trace_incomplete', 'timeout_fired'].every(k => typeof v[k] === 'boolean')
      || !optional(v.exit_code, n => Number.isSafeInteger(n) && Math.abs(n) <= 2 ** 32 - 1)
      || !optional(v.signal, n => ['SIGTERM', 'SIGKILL'].includes(n))
      || !ERRORS.includes(v.operation_error_kind)
      || !['stream', 'buffered', 'unavailable'].includes(v.trace_source)
      || !['complete', 'empty', 'invalid', 'incomplete', 'limit_exceeded', 'unavailable'].includes(v.trace_status)
      || !integer(v.trace_bytes, 4097) || !['granted', 'denied', 'unavailable'].includes(v.permission_state)
      || !exact(v.child, ['present', 'pid', 'spawn_observed', 'error_kind', 'close_observed', 'close_exit_code', 'close_signal'])) return false;
  const child = v.child;
  return ['present', 'spawn_observed', 'close_observed'].every(k => typeof child[k] === 'boolean')
    && optional(child.pid, n => integer(n, 2 ** 32 - 1) && n > 0)
    && optional(child.error_kind, n => ERRORS.includes(n))
    && optional(child.close_exit_code, n => Number.isSafeInteger(n) && Math.abs(n) <= 2 ** 32 - 1)
    && optional(child.close_signal, n => SIGNALS.includes(n));
}

/** Extract only a closed diagnostic; never retain arbitrary error text. */
export function startupDiagnosticFromMessage(message) {
  if (typeof message !== 'string' || message.length > 16_384) return null;
  const text = message.startsWith('Error: ') ? message.slice(7) : message;
  for (const [prefix, native] of [['Brain startup failed: ', false], ['Native permission interaction failed: ', true]]) {
    if (!text.startsWith(prefix)) continue;
    try {
      const raw = text.slice(prefix.length); const value = JSON.parse(raw);
      if (JSON.stringify(value) !== raw) return null;
      if (native && !validNativeEnvelope(value)) return null;
      const summary = native ? value.startup : value;
      return validStartupDiagnostic(summary) ? summary : null;
    } catch { return null; }
  }
  return null;
}

function errorKind(error) {
  if (!error) return 'other';
  if (error.code === 'ABORT_ERR') return 'aborted';
  if (error.code === 'ENOENT') return 'not_found';
  if (['EACCES', 'EPERM'].includes(error.code)) return 'access_denied';
  if (error.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') return 'output_limit';
  return 'other';
}

/** Observation only: no waits, process control, health decisions or raw log retention. */
export function createStartupObservations(owner, { now = () => performance.now() } = {}) {
  if (!Object.hasOwn(PHASES, owner)) throw new Error('startup_owner_invalid');
  const started = now();
  const elapsed = () => Math.min(MAX_MS, Math.max(0, Math.round(now() - started)));
  const state = { schema: STARTUP_SCHEMA, owner, reason: owner === 'brain' ? 'unhealthy' : 'interaction_failed',
    elapsed_ms: 0, pid: null, spawn_ms: null, first_stdout_ms: null, first_stderr_ms: null,
    stdout_bytes: 0, stderr_bytes: 0, exit_ms: null, close_ms: null, exit_code: null, close_exit_code: null,
    signal: null, close_signal: null, error_kind: null, phases: [], phase_status: 'empty',
    health_attempts: 0, health_outcome: 'not_attempted' };
  const decoder = new StringDecoder('utf8'); let pending = ''; let parsedBytes = 0;
  const phase = name => {
    if (PHASES[owner].includes(name) && !state.phases.some(p => p.phase === name)) {
      state.phases.push({ phase: name, elapsed_ms: elapsed() });
      if (state.phase_status !== 'limited') state.phase_status = 'observed';
    }
  };
  function bytes(kind, chunk) {
    const size = typeof chunk === 'string' ? Buffer.byteLength(chunk) : chunk.length;
    if (size && state['first_' + kind + '_ms'] === null) state['first_' + kind + '_ms'] = elapsed();
    state[kind + '_bytes'] = Math.min(MAX_BYTES, state[kind + '_bytes'] + size);
    if (owner !== 'brain' || kind !== 'stdout' || state.phase_status === 'limited') return;
    const remaining = 4096 - parsedBytes;
    const prefix = Buffer.from(chunk).subarray(0, remaining);
    parsedBytes += prefix.length; pending += decoder.write(prefix);
    let index;
    while ((index = pending.indexOf('\n')) !== -1) {
      const line = pending.slice(0, index).trim(); pending = pending.slice(index + 1);
      try {
        const value = JSON.parse(line);
        if (exact(value, ['schema', 'phase']) && value.schema === 'browser-startup-phase/v1') phase(value.phase);
      } catch { /* Unstructured output is counted, never copied into observations. */ }
    }
    if (size > remaining) { state.phase_status = 'limited'; pending = ''; }
  }
  function bind(child) {
    state.pid = Number.isSafeInteger(child.pid) && child.pid > 0 ? child.pid : null;
    const stdout = chunk => bytes('stdout', chunk); const stderr = chunk => bytes('stderr', chunk);
    const spawn = () => { state.spawn_ms = elapsed(); };
    const error = value => { state.error_kind = errorKind(value); };
    const code = value => Number.isSafeInteger(value) && Math.abs(value) <= 2 ** 32 - 1 ? value : null;
    const signal = value => value == null ? null : SIGNALS.includes(value) ? value : 'other';
    const exit = (value, sig) => { state.exit_ms = elapsed(); state.exit_code = code(value); state.signal = signal(sig); };
    const close = (value, sig) => { state.close_ms = elapsed(); state.close_exit_code = code(value); state.close_signal = signal(sig); cleanup(); };
    function cleanup() {
      child.stdout?.removeListener('data', stdout); child.stderr?.removeListener('data', stderr);
      child.removeListener('spawn', spawn); child.removeListener('error', error);
      child.removeListener('exit', exit); child.removeListener('close', close);
    }
    child.stdout?.on('data', stdout); child.stderr?.on('data', stderr);
    child.on('spawn', spawn); child.on('error', error); child.on('exit', exit); child.on('close', close);
    return cleanup;
  }
  return { bind, phase,
    limitPhases() { state.phase_status = 'limited'; },
    failure(error) { state.error_kind = errorKind(error); },
    health(outcome) {
      if (owner === 'brain' && HEALTH.includes(outcome) && outcome !== 'not_attempted') {
        state.health_attempts = Math.min(10_000, state.health_attempts + 1); state.health_outcome = outcome;
      }
    },
    snapshot(reason = state.reason) { return structuredClone({ ...state, reason, elapsed_ms: elapsed() }); },
  };
}
