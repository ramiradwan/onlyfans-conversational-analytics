/** Observational access to the installed worker's existing module scope.
 *
 * Uses its existing diagnostic function as a short debugger pause point; never
 * imports/replaces the background entry or invokes private coordinator work.
 * Callers must project public counters/state and use readonly storage methods.
 * Detach before testing worker termination: an attached debugger keeps it alive.
 */
export async function attachWorkerInspector(browserCDP, workerUrl) {
  const { targetInfos } = await browserCDP.send('Target.getTargets');
  const target = targetInfos.find((entry) => entry.type === 'service_worker' && entry.url === workerUrl);
  if (!target) throw new Error('Installed qualification worker is unavailable');
  const { sessionId } = await browserCDP.send('Target.attachToTarget', { targetId: target.targetId, flatten: false });
  let sequence = 0;
  let closed = false;
  let queue = Promise.resolve();
  const pending = new Map();
  const listeners = new Map();
  const received = (event) => {
    if (event.sessionId !== sessionId) return;
    const message = JSON.parse(event.message);
    if (message.id !== undefined) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id); clearTimeout(request.timer);
      if (message.error) request.reject(new Error('Installed worker inspection protocol refused'));
      else request.resolve(message.result ?? {});
    } else {
      for (const listener of listeners.get(message.method) ?? []) listener(message.params ?? {});
    }
  };
  browserCDP.on('Target.receivedMessageFromTarget', received);
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    if (closed) { reject(new Error('Installed worker inspector closed')); return; }
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id); reject(new Error('Installed worker inspection timed out'));
    }, 10_000);
    pending.set(id, { resolve, reject, timer });
    void browserCDP.send('Target.sendMessageToTarget', {
      sessionId, message: JSON.stringify({ id, method, params }),
    }).catch(() => {
      const request = pending.get(id);
      if (request) { pending.delete(id); clearTimeout(timer); reject(new Error('Installed worker inspection detached')); }
    });
  });
  await send('Runtime.enable');
  await send('Debugger.enable');

  const inspect = (expression) => {
    if (typeof expression !== 'string' || expression.length > 16_384) throw new TypeError('Bounded readonly projection required');
    const running = queue.then(async () => {
      let breakpoint = null;
      let paused = false;
      let trigger = null;
      let pauseTimer;
      let pauseListener;
      let watchdog;
      try {
        const functionResult = await send('Runtime.evaluate', {
          expression: 'globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__', returnByValue: false,
        });
        if (!functionResult.result?.objectId || functionResult.result.type !== 'function') {
          throw new Error('Installed diagnostic scope is unavailable');
        }
        ({ breakpointId: breakpoint } = await send('Debugger.setBreakpointOnFunctionCall', { objectId: functionResult.result.objectId }));
        const stopped = new Promise((resolve, reject) => {
          pauseListener = (event) => { paused = true; resolve(event); };
          listeners.set('Debugger.paused', new Set([pauseListener]));
          pauseTimer = setTimeout(() => reject(new Error('Installed diagnostic pause unavailable')), 5_000);
        });
        trigger = send('Runtime.evaluate', {
          expression: 'globalThis.__OFCA_AGENT_DIAGNOSTIC_SNAPSHOT__()', returnByValue: true,
        });
        // Observe the pending call even when a different protocol step fails.
        trigger.catch(() => undefined);
        const event = await stopped;
        clearTimeout(pauseTimer);
        watchdog = setTimeout(() => { if (paused) void send('Debugger.resume').catch(() => undefined); }, 5_000);
        const frame = event.callFrames?.[0];
        if (!frame) throw new Error('Installed diagnostic call frame is unavailable');
        const evaluated = await send('Debugger.evaluateOnCallFrame', {
          callFrameId: frame.callFrameId, expression, returnByValue: false, silent: true,
        });
        if (evaluated.exceptionDetails) throw new Error('Installed readonly projection refused');
        // A projection may read the diagnostic again after its first await.
        // Remove this one-shot pause before resuming any asynchronous work.
        await send('Debugger.removeBreakpoint', { breakpointId: breakpoint });
        breakpoint = null;
        await send('Debugger.resume'); paused = false;
        clearTimeout(watchdog);
        await trigger;
        if (evaluated.result?.subtype === 'promise') {
          const awaited = await send('Runtime.awaitPromise', { promiseObjectId: evaluated.result.objectId, returnByValue: true });
          if (awaited.exceptionDetails) throw new Error('Installed readonly projection failed');
          return awaited.result?.value;
        }
        if (evaluated.result?.objectId) {
          const value = await send('Runtime.callFunctionOn', {
            objectId: evaluated.result.objectId, functionDeclaration: 'function () { return this; }', returnByValue: true,
          });
          if (value.exceptionDetails) throw new Error('Installed readonly projection failed');
          return value.result?.value;
        }
        return evaluated.result?.value;
      } finally {
        clearTimeout(pauseTimer); clearTimeout(watchdog);
        listeners.get('Debugger.paused')?.delete(pauseListener);
        if (paused) await send('Debugger.resume').catch(() => undefined);
        if (breakpoint) await send('Debugger.removeBreakpoint', { breakpointId: breakpoint }).catch(() => undefined);
        if (trigger) await trigger.catch(() => undefined);
      }
    });
    queue = running.catch(() => undefined);
    return running;
  };
  inspect.close = async () => {
    await queue;
    if (closed) return;
    await send('Debugger.disable').catch(() => undefined);
    closed = true;
    for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error('Installed worker inspector closed')); }
    pending.clear();
    browserCDP.off('Target.receivedMessageFromTarget', received);
    await browserCDP.send('Target.detachFromTarget', { sessionId }).catch(() => undefined);
  };
  return inspect;
}
