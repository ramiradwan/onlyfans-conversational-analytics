function frozenTabError() {
  const error = new Error('OnlyFans tab frozen state is unavailable or unsafe for history sync.');
  error.code = 'frozen_tab_unavailable';
  return error;
}

function selectedOnlyFansTab(tabs) {
  return (tabs ?? []).find((candidate) => candidate.active === true && Number.isInteger(candidate.id))
    ?? (tabs ?? []).find((candidate) => Number.isInteger(candidate.id))
    ?? null;
}

/**
 * Chrome 132 introduced Tab.frozen. Older Chrome cannot prove that page-world
 * work will run, so history signing fails closed instead of treating absence as
 * an unfrozen tab.
 */
export async function assertOnlyFansTabCanRun(chromeApi, tabId = null) {
  if (!chromeApi?.tabs?.query || !chromeApi.tabs.get) throw frozenTabError();
  let resolvedTabId = tabId;
  if (!Number.isInteger(resolvedTabId)) {
    const tab = selectedOnlyFansTab(await chromeApi.tabs.query({
      url: ['https://onlyfans.com/*'],
    }));
    if (tab === null) throw new Error('No authenticated OnlyFans tab is open.');
    resolvedTabId = tab.id;
  }
  const tab = await chromeApi.tabs.get(resolvedTabId);
  // Do not infer "not frozen" from a pre-Chrome-132 Tab object.
  if (!Object.hasOwn(tab ?? {}, 'frozen') || tab.frozen !== false) throw frozenTabError();
  return resolvedTabId;
}

/** Adds a second pre-dispatch fence in case the selected tab changes after signing starts. */
export function guardMainWorldDispatch(chromeApi) {
  if (!chromeApi?.scripting?.executeScript) throw frozenTabError();
  const scripting = new Proxy(chromeApi.scripting, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (property === 'executeScript') {
        return async (details) => {
          const tabId = details?.target?.tabId;
          await assertOnlyFansTabCanRun(chromeApi, tabId);
          return value.call(target, details);
        };
      }
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  return new Proxy(chromeApi, {
    get(target, property, receiver) {
      return property === 'scripting' ? scripting : Reflect.get(target, property, receiver);
    },
  });
}
