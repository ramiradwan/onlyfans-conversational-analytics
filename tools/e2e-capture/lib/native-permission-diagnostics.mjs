import { StringDecoder } from 'node:string_decoder';
import { createStartupObservations } from './startup-diagnostics.mjs';

const SCHEMA = 'native-permission-diagnostic/v1';
const STAGES = new Set(['started', 'window_found', 'prompt_found', 'invoke_attempted', 'allow_returned', 'prompt_dismissed']);
const FLAGS = ['window_found', 'prompt_found', 'invoke_attempted', 'allow_invoked', 'prompt_dismissed'];
const KINDS = new Set(['preview', 'full', 'history', 'unspecified']);
const TRACE_BYTES = 4096;
const TRACE_RECORDS = 8;

function closedTraceRecord(entry) {
  if (!entry || Array.isArray(entry) || entry.schema !== SCHEMA || !STAGES.has(entry.stage)
      || Object.keys(entry).length !== FLAGS.length + 2
      || FLAGS.some((flag) => typeof entry[flag] !== 'boolean')) return null;
  return { schema: SCHEMA, stage: entry.stage, ...Object.fromEntries(FLAGS.map(flag => [flag, entry[flag]])) };
}

export function parseNativePermissionTrace(value) {
  if (typeof value !== 'string' || value.length > 4096) return null;
  const lines = value.trim().split(/\r?\n/u);
  if (lines.length > 8) return null;
  let last = null;
  for (const line of lines) {
    let entry;
    try { entry = JSON.parse(line); } catch { return null; }
    last = closedTraceRecord(entry);
    if (!last) return null;
  }
  return last;
}

function traceCollector(observePhase) {
  const decoder = new StringDecoder('utf8');
  let pending = '', last = null, bytes = 0, records = 0;
  let invalid = false, incomplete = false, limited = false;
  function record(line, final = false) {
    if (!line.trim()) return;
    if (++records > TRACE_RECORDS) { limited = true; return; }
    let parsed;
    try { parsed = JSON.parse(line); }
    catch { if (final) incomplete = true; else invalid = true; return; }
    const closed = closedTraceRecord(parsed);
    if (closed) { last = closed; observePhase(closed.stage); }
    else invalid = true;
  }
  return {
    write(chunk) {
      if (limited) return;
      const length = typeof chunk === 'string' ? Buffer.byteLength(chunk) : chunk.length;
      const remaining = TRACE_BYTES - bytes;
      const prefix = typeof chunk === 'string'
        ? Buffer.from(chunk.slice(0, remaining), 'utf8').subarray(0, remaining)
        : chunk.subarray(0, remaining);
      bytes = Math.min(TRACE_BYTES + 1, bytes + length);
      pending += decoder.write(prefix);
      let newline;
      while (!limited && (newline = pending.indexOf('\n')) !== -1) {
        record(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
      }
      if (length > remaining) limited = true;
      if (limited) pending = '';
    },
    finish() {
      if (!limited) record(pending + decoder.end(), true);
      pending = '';
      return { last, bytes, incomplete, status: limited ? 'limit_exceeded'
        : invalid ? 'invalid' : incomplete ? 'incomplete' : last ? 'complete' : 'empty' };
    },
  };
}

function errorKind(error) {
  if (!error) return null;
  switch (error.code) {
    case 'ABORT_ERR': return 'aborted';
    case 'ENOENT': return 'not_found';
    case 'EACCES': case 'EPERM': return 'access_denied';
    case 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER': return 'output_limit';
    default: return 'other';
  }
}

function closedSignal(signal) {
  return signal == null ? null : ['SIGTERM', 'SIGKILL'].includes(signal) ? signal : 'other';
}

async function readPermissionOnce(read, schedule, cancel) {
  if (typeof read !== 'function') return 'unavailable';
  let timer;
  try {
    const value = await Promise.race([
      Promise.resolve().then(read),
      new Promise((resolve) => { timer = schedule(() => resolve(null), 1000); }),
    ]);
    return value === true ? 'granted' : value === false ? 'denied' : 'unavailable';
  } catch { return 'unavailable'; }
  finally { cancel(timer); }
}

/** Preserve a failed native interaction while recording only closed metadata. */
export async function runNativePermissionInteraction({
  run, readPermission, requestKind = 'unspecified', schedule = setTimeout, cancel = clearTimeout,
  now = () => performance.now(), startupAllowanceMs = 0,
}) {
  if (!Number.isSafeInteger(startupAllowanceMs) || startupAllowanceMs < 0 || startupAllowanceMs > 15_000) {
    throw new RangeError('native_permission_startup_allowance_invalid');
  }
  const startup = createStartupObservations('native_permission', { now });
  const controller = new AbortController();
  let timeoutFired = false;
  let failed = false;
  let failure, result, command, child, close, onSpawn, onError, onClose;
  let streamed = false;
  const collector = traceCollector(startup.phase);
  const onData = chunk => collector.write(chunk);
  const childState = { present: false, pid: null, spawn_observed: false, error_kind: null,
    close_observed: false, close_exit_code: null, close_signal: null };
  const timer = schedule(() => { timeoutFired = true; controller.abort(); }, 15_000 + startupAllowanceMs);
  try {
    command = run(controller.signal);
    child = command?.child;
    if (child) {
      startup.bind(child);
      childState.present = true;
      childState.pid = Number.isSafeInteger(child.pid) && child.pid > 0 ? child.pid : null;
      onSpawn = () => { childState.spawn_observed = true; };
      onError = error => { childState.error_kind = errorKind(error); };
      close = new Promise(resolve => {
        onClose = (code, signal) => {
          childState.close_observed = true;
          childState.close_exit_code = Number.isSafeInteger(code) ? code : null;
          childState.close_signal = closedSignal(signal);
          resolve();
        };
        child.once('close', onClose);
      });
      child.once('spawn', onSpawn);
      child.on('error', onError);
      if (child.stdout) { streamed = true; child.stdout.on('data', onData); }
    }
    result = await command;
  }
  catch (error) { failed = true; failure = error; startup.failure(error); }
  finally {
    // An AbortError reports cancellation; only close joins the owned child and its pipes.
    await close;
    cancel(timer);
    child?.stdout?.removeListener('data', onData);
    if (child) {
      child.removeListener('spawn', onSpawn);
      child.removeListener('error', onError);
      child.removeListener('close', onClose);
    }
  }
  if (!failed && !timeoutFired) return result;

  const buffered = !streamed && typeof failure?.stdout === 'string';
  if (buffered) collector.write(failure.stdout);
  const observed = collector.finish();
  if (observed.status === 'limit_exceeded') startup.limitPhases();
  const trace = observed.last;
  const diagnostic = {
    schema: SCHEMA,
    request_kind: KINDS.has(requestKind) ? requestKind : 'unspecified',
    stage: trace?.stage ?? 'unknown',
    ...Object.fromEntries(FLAGS.map((flag) => [flag, trace?.[flag] ?? false])),
    exit_code: Number.isSafeInteger(failure?.code) ? failure.code : null,
    signal: ['SIGTERM', 'SIGKILL'].includes(failure?.signal) ? failure.signal : null,
    killed: failure?.killed === true,
    operation_error_kind: !failed && timeoutFired ? 'aborted' : errorKind(failure) ?? 'other',
    trace_source: streamed ? 'stream' : buffered ? 'buffered' : 'unavailable',
    trace_status: streamed || buffered ? observed.status : 'unavailable',
    trace_incomplete: observed.incomplete,
    trace_bytes: observed.bytes,
    child: childState,
    timeout_fired: timeoutFired,
    permission_state: await readPermissionOnce(readPermission, schedule, cancel),
    startup: startup.snapshot(),
  };
  // Do not retain the subprocess error as a cause: it contains the full command.
  throw new Error(`Native permission interaction failed: ${JSON.stringify(diagnostic)}`);
}
