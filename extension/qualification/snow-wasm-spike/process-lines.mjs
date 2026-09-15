export function createLineQueue(child, label = 'child') {
  if (!child?.stdout) throw new TypeError(`${label}_stdout_unavailable`);
  child.stdout.setEncoding('utf8');
  let buffer = '';
  const lines = [];
  const waiters = [];
  let terminalError = null;

  const settleLine = (line) => {
    const waiter = waiters.shift();
    if (waiter) waiter.resolve(line);
    else lines.push(line);
  };
  const fail = (error) => {
    if (terminalError) return;
    terminalError = error;
    while (waiters.length) waiters.shift().reject(error);
  };
  const drain = () => {
    for (;;) {
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      const raw = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      settleLine(raw.endsWith('\r') ? raw.slice(0, -1) : raw);
    }
  };

  child.stdout.on('data', (chunk) => { buffer += chunk; drain(); });
  child.once('error', (error) => fail(new Error(`${label}_process_error:${error.message}`)));
  child.once('exit', (code, signal) => {
    if (buffer.length) { settleLine(buffer); buffer = ''; }
    fail(new Error(`${label}_exited:code=${code ?? 'null'}:signal=${signal ?? 'null'}`));
  });

  return {
    nextLine(timeoutMs = 5000) {
      if (lines.length) return Promise.resolve(lines.shift());
      if (terminalError) return Promise.reject(terminalError);
      return new Promise((resolve, reject) => {
        const waiter = {
          resolve: (line) => { clearTimeout(timer); resolve(line); },
          reject: (error) => { clearTimeout(timer); reject(error); },
        };
        const timer = setTimeout(() => {
          const index = waiters.indexOf(waiter);
          if (index >= 0) waiters.splice(index, 1);
          reject(new Error(`${label}_line_timeout`));
        }, timeoutMs);
        waiters.push(waiter);
      });
    },
  };
}

export async function within(label, action, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(action),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label}_timeout`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

// Chrome <143 exposes a worker execution context before its module is loaded.
// Keep clocks and timers in the controller; worker readiness reads are scalar
// evaluations that do not hold an awaitPromise operation across module startup.
// This only waits for the entry point, never retries qualification operations.
export async function waitForWorkerEntry(worker, entry, timeoutMs = 5000) {
  const deadline = performance.now() + timeoutMs;
  for (;;) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new Error('worker_initialization_timeout');
    const ready = await within('worker_initialization',
      () => worker.evaluate((name) => typeof globalThis[name] === 'function', entry), remaining);
    if (performance.now() >= deadline) throw new Error('worker_initialization_timeout');
    if (ready) return;
    await new Promise((resolve) => setTimeout(resolve, Math.min(10, deadline - performance.now())));
  }
}

export async function stopChild(child, label = 'child') {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((resolve) => child.once('exit', resolve));
  child.kill('SIGTERM');
  try {
    await within(`${label}_terminate`, () => exited, 3000);
  } catch {
    child.kill('SIGKILL');
    await within(`${label}_kill`, () => exited, 3000).catch(() => {});
  }
}
