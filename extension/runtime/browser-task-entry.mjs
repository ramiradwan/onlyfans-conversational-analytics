import { exact, isTask, UUID } from '../../shared/browser-tasks/wire.mjs';

export const BROWSER_TASK_ENTRY_KEY = 'browser_task_entry_v1';
export const BROWSER_TASK_ADOPTION_KEY = 'browser_task_adoption_v1';
export const BROWSER_TASK_INSTANCE_KEY = 'browser_task_instance_v1';
const sameScope = (left, right) => left?.scope_id === right?.scope_id
  && left?.disclosure_bundle_id === right?.disclosure_bundle_id;
const validScope = value => exact(value, ['scope_id', 'disclosure_bundle_id'])
  && UUID.test(value.scope_id) && /^[a-f0-9]{64}$/u.test(value.disclosure_bundle_id);

export function desktopAdopted(value, record, instance) {
  return exact(value, ['journey_id', 'instance_id', 'draft_scope']) && UUID.test(instance)
    && value.instance_id === instance && UUID.test(value.journey_id) && value.journey_id === record?.journey_id
    && validScope(value.draft_scope) && sameScope(value.draft_scope, record.draft_scope);
}

/** One bounded presentation for an already admitted browser access task. */
export function createBrowserTaskEntry({ storage, now = Date.now }) {
  // Chrome can end a worker without delivering the external port's disconnect.
  // A new worker owns no admitted helper yet, even though session storage survived.
  const reset = Promise.resolve().then(async () => {
    if ((await storage.get(BROWSER_TASK_ENTRY_KEY))[BROWSER_TASK_ENTRY_KEY] !== undefined)
      await storage.remove(BROWSER_TASK_ENTRY_KEY);
  });
  void reset.catch(() => undefined);
  let queue = reset;
  const serial = work => { const result = queue.then(() => reset).then(work); queue = result.catch(() => undefined); return result; };
  const valid = value => exact(value, ['task', 'draft_scope']) && isTask(value.task)
    && ['browser.access', 'browser.identity'].includes(value.task.purpose)
    && ['pending', 'accepted', 'outcome_unknown'].includes(value.task.state)
    && value.task.expires_at * 1000 > now() && validScope(value.draft_scope);
  return Object.freeze({
    open: (task, draft_scope, authorize) => serial(async () => {
      const entry = { task, draft_scope };
      if (!valid(entry)) throw Error('task_expired');
      await authorize();
      if (!valid(entry)) throw Error('task_expired');
      await storage.set({ [BROWSER_TASK_ENTRY_KEY]: entry });
    }),
    read: record => serial(async () => {
      const entry = (await storage.get(BROWSER_TASK_ENTRY_KEY))[BROWSER_TASK_ENTRY_KEY];
      if (!valid(entry)) {
        if (entry !== undefined) await storage.remove(BROWSER_TASK_ENTRY_KEY);
        return null;
      }
      return entry.task.journey_id === record?.journey_id && sameScope(entry.draft_scope, record.draft_scope) ? entry : null;
    }),
    close: task => serial(async () => {
      const entry = (await storage.get(BROWSER_TASK_ENTRY_KEY))[BROWSER_TASK_ENTRY_KEY];
      if (entry?.task?.task_id === task.task_id && entry.task.admission_generation === task.admission_generation)
        await storage.remove(BROWSER_TASK_ENTRY_KEY);
    }),
  });
}
