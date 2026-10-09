const SCHEMA = 'native-permission-diagnostic/v1';
const STAGES = new Set(['started', 'window_found', 'prompt_found', 'invoke_attempted', 'allow_returned', 'prompt_dismissed']);
const FLAGS = ['window_found', 'prompt_found', 'invoke_attempted', 'allow_invoked', 'prompt_dismissed'];
const KINDS = new Set(['preview', 'full', 'history', 'unspecified']);

export function parseNativePermissionTrace(value) {
  if (typeof value !== 'string' || value.length > 4096) return null;
  const lines = value.trim().split(/\r?\n/u);
  if (lines.length > 8) return null;
  let last = null;
  for (const line of lines) {
    let entry;
    try { entry = JSON.parse(line); } catch { return null; }
    if (!entry || Array.isArray(entry) || entry.schema !== SCHEMA || !STAGES.has(entry.stage)
        || Object.keys(entry).length !== FLAGS.length + 2
        || FLAGS.some((flag) => typeof entry[flag] !== 'boolean')) return null;
    last = entry;
  }
  return last;
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
}) {
  const controller = new AbortController();
  let timeoutFired = false;
  let failure;
  const timer = schedule(() => { timeoutFired = true; controller.abort(); }, 15_000);
  try { return await run(controller.signal); }
  catch (error) { failure = error; }
  finally { cancel(timer); }

  const trace = parseNativePermissionTrace(failure?.stdout);
  const diagnostic = {
    schema: SCHEMA,
    request_kind: KINDS.has(requestKind) ? requestKind : 'unspecified',
    stage: trace?.stage ?? 'unknown',
    ...Object.fromEntries(FLAGS.map((flag) => [flag, trace?.[flag] ?? false])),
    exit_code: Number.isSafeInteger(failure?.code) ? failure.code : null,
    signal: ['SIGTERM', 'SIGKILL'].includes(failure?.signal) ? failure.signal : null,
    killed: failure?.killed === true,
    timeout_fired: timeoutFired,
    permission_state: await readPermissionOnce(readPermission, schedule, cancel),
  };
  // Do not retain the subprocess error as a cause: it contains the full command.
  throw new Error(`Native permission interaction failed: ${JSON.stringify(diagnostic)}`);
}
