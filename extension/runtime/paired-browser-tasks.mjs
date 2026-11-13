import { isPayload, isObservation } from '../../shared/browser-tasks/wire.mjs';
import { createAuthenticationTaskSession } from './authentication-task-session.mjs';

/** Explicit authenticated dispatch only; reconnect and reads never open a window. */
export function createPairedBrowserTasks({ owner, authentication, now = Date.now }) {
  const active = new Map();
  let queue = Promise.resolve();
  return Object.freeze({
    run(payload, controls) {
      const work = queue.then(async () => {
        if (!isPayload(payload) || payload.task.purpose !== 'browser.authentication'
          || payload.creator_account_id !== controls.creatorAccountId) throw Error('task_changed');
        const deadline = Math.min(payload.task.expires_at * 1000, Date.parse(payload.ceremony.expires_at));
        for (const [id, entry] of active) if (entry.deadline <= now() || entry.signal.aborted) { entry.close(); active.delete(id); }
        if (deadline <= now() || deadline - now() > 1800000) throw Error('task_expired');
        controls.guard();
        const prior = active.get(payload.task.task_id);
        if (!prior && active.size >= 8) throw Error('unavailable');
        prior?.close();
        const context = await owner.attach(payload, { authorize: controls.guard, signal: controls.signal });
        if (controls.signal.aborted) { await owner.release?.(context); controls.guard(); }
        const guard = async () => {
          controls.guard();
          if (deadline <= now()) throw Error('task_expired');
          const state = await owner.readPaired(context, controls.creatorAccountId);
          controls.guard();
          if (!isObservation(state) || !['ready', 'paused'].includes(state.state)
            || state.creator_account_id !== payload.creator_account_id) throw Error('task_changed');
        };
        let session;
        try {
          session = createAuthenticationTaskSession({ owner, context, authentication, payload,
            authorize: controls.guard, signal: controls.signal, deadline, now,
            publish: state => state.state === 'idle' ? undefined : controls.notify(state),
            onClose: () => { if (active.get(payload.task.task_id)?.context === context) active.delete(payload.task.task_id); } });
          active.set(payload.task.task_id, { deadline, signal: controls.signal, context, close: session.close, payload });
          await authentication.launch(payload, guard); await session.refresh();
        }
        catch {
          if (session) session.close();
          else await owner.release?.(context);
          controls.guard();
          await controls.notify({ state: 'checking', notification: { ceremony_id: payload.ceremony.ceremony_id,
            attempt_id: payload.ceremony.attempt_id, notification: 'unavailable' } });
        }
      });
      queue = work.catch(() => undefined);
      return work;
    },
    /** Brain says this task ended. Only an exact task and generation match removes its popup. */
    end({ task_id, admission_generation }) {
      const work = queue.then(async () => {
        const entry = active.get(task_id);
        if (entry) {
          if (entry.payload.task.admission_generation !== admission_generation) return;
          active.delete(task_id);
          await authentication.retire(entry.payload);
          entry.close();
          return;
        }
        // A restarted worker holds no entry. The popup's saved window decides.
        await authentication.retireTask?.({ task_id, admission_generation });
      });
      queue = work.catch(() => undefined);
      return work;
    },
    dispose() { for (const entry of active.values()) entry.close(); },
  });
}
