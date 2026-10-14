/** Read/notify lifetime for one admitted auth task; never launches a window. */
export function createAuthenticationTaskSession({ owner, context, authentication, payload,
  authorize, publish, signal, deadline, now = Date.now, changed = null, onClose = () => {} }) {
  if (!Number.isFinite(deadline) || deadline <= now() || deadline - now() > 1800000) throw Error('task_expired');
  let closed = false, encoded = null, serial = Promise.resolve(), unsubscribe = () => {}, timer;
  const close = (reason = 'closed') => {
    if (closed) return;
    closed = true; clearTimeout(timer); unsubscribe();
    signal.removeEventListener('abort', abort);
    void Promise.resolve(owner.release?.(context)).catch(() => undefined);
    onClose(reason);
  };
  const abort = () => close();
  const refresh = () => {
    const work = serial.then(async () => {
      if (closed || deadline <= now()) return;
      await authorize();
      const state = await authentication.read(payload);
      if (closed || deadline <= now()) return;
      await authorize();
      if (closed || deadline <= now()) return;
      const next = JSON.stringify(state);
      if (next === encoded) return;
      await publish(state);
      if (!closed) encoded = next;
    });
    serial = work.catch(() => undefined);
    return work;
  };
  unsubscribe = authentication.subscribe(changed ?? (() => { void refresh().catch(() => undefined); }));
  timer = setTimeout(() => close('expired'), deadline - now());
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) close();
  return Object.freeze({ refresh, invalidate() { encoded = null; }, close });
}
