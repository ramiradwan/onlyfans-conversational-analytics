const HELPER_ORIGIN = 'http://bridge.localhost:17871/';

// A finished tab load can change what a sign-in reads. The extension's own pages and the desktop helper cannot.
export function tabChangeSource(chromeApi) {
  const own = `chrome-extension://${chromeApi.runtime.id}/`;
  return Object.assign((changed) => {
    const listener = (_tabId, changeInfo, tab) => {
      if (changeInfo?.status !== 'complete') return;
      const url = tab?.url;
      if (typeof url === 'string' && (url.startsWith(own) || url.startsWith(HELPER_ORIGIN))) return;
      changed();
    };
    chromeApi.tabs?.onUpdated?.addListener(listener);
    return () => chromeApi.tabs?.onUpdated?.removeListener?.(listener);
  }, { label: 'tab' });
}
