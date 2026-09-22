function frozenTabError() {
  const error = new Error('OnlyFans tab frozen state is unavailable or unsafe for history sync.');
  error.code = 'frozen_tab_unavailable';
  return error;
}

function selectedOnlyFansTab(tabs) {
  const runnable = (tabs ?? []).filter(isRunnableTab);
  return runnable.find((candidate) => candidate.active === true)
    ?? runnable[0]
    ?? null;
}

function isRunnableTab(tab) {
  return Number.isInteger(tab?.id) && Object.hasOwn(tab, 'frozen') && tab.frozen === false;
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
    const tabs = await chromeApi.tabs.query({
      url: ['https://onlyfans.com/*'],
    });
    const tab = selectedOnlyFansTab(tabs);
    if (tab === null) {
      if ((tabs ?? []).some((candidate) => Number.isInteger(candidate?.id))) throw frozenTabError();
      throw new Error('No authenticated OnlyFans tab is open.');
    }
    resolvedTabId = tab.id;
  }
  const tab = await chromeApi.tabs.get(resolvedTabId);
  // Do not infer "not frozen" from a pre-Chrome-132 Tab object.
  if (!Object.hasOwn(tab ?? {}, 'frozen') || tab.frozen !== false) throw frozenTabError();
  return resolvedTabId;
}

/** Adds a second pre-dispatch fence in case the selected tab changes after signing starts. */
export function guardMainWorldDispatch(chromeApi, { signal } = {}) {
  if (!chromeApi?.scripting?.executeScript || !chromeApi?.tabs?.query) throw frozenTabError();
  // The signer's initial read and inactive-tab refresh selection must see the
  // same runnable candidates as preflight. A sleeping older tab must not hide
  // an available document. Exact-target checks below still reject a tab that
  // freezes after selection; they never switch an in-flight operation's tab.
  const tabs = new Proxy(chromeApi.tabs, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (property === 'query') {
        return async (query) => {
          signal?.throwIfAborted();
          const candidates = await value.call(target, query);
          signal?.throwIfAborted();
          return (candidates ?? []).filter(isRunnableTab);
        };
      }
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  const scripting = new Proxy(chromeApi.scripting, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (property === 'executeScript') {
        return async (details) => {
          signal?.throwIfAborted();
          const tabId = details?.target?.tabId;
          await assertOnlyFansTabCanRun(chromeApi, tabId);
          signal?.throwIfAborted();
          return value.call(target, details);
        };
      }
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  return new Proxy(chromeApi, {
    get(target, property, receiver) {
      if (property === 'scripting') return scripting;
      if (property === 'tabs') return tabs;
      return Reflect.get(target, property, receiver);
    },
  });
}
