// Packaged UI admission and navigation. These checks do not authorize capture.
export const UI_OPEN_SURFACE_MESSAGE_TYPE = 'ofca.ui.open-surface';
const PAGES = Object.freeze(['popup', 'setup', 'options']);

export function uiSurface(sender, chromeApi) {
  if (sender?.id !== chromeApi.runtime.id || typeof sender.url !== 'string'
    || (sender.frameId !== undefined && sender.frameId !== 0)) return null;
  return PAGES.find((page) => {
    const base = chromeApi.runtime.getURL(`${page}.html`);
    return sender.url === base || sender.url.startsWith(`${base}#`);
  }) ?? null;
}

export function allowsUiMessage(sender, message, chromeApi) {
  const surface = uiSurface(sender, chromeApi);
  if (!surface) return false;
  if (message?.type === 'ofca.ui.delete-local-data' || message?.type === 'ofca.ui.clear-preview') {
    return surface === 'options';
  }
  if (typeof message?.type === 'string' && message.type.startsWith('ofca.legal-activation.')
    && message.type !== 'ofca.legal-activation.status') return surface === 'setup';
  if (message?.type === 'ofca.ui.transition') {
    if (surface === 'popup') return ['pause', 'resume'].includes(message.mode);
    if (message.mode === 'revoked') return surface === 'options';
  }
  return true;
}

// One worker serializes opens from all extension pages. Focusing never reloads
// an existing setup page or replaces the attempt it owns.
export function registerSurfaceNavigation(chromeApi = globalThis.chrome) {
  let queue = Promise.resolve();
  chromeApi.runtime.onMessage.addListener((message, sender, reply) => {
    if (message?.type !== UI_OPEN_SURFACE_MESSAGE_TYPE || !uiSurface(sender, chromeApi)) return false;
    if (Object.keys(message).sort().join(',') !== 'section,surface,type'
      || !['setup', 'options'].includes(message.surface)
      || !(message.surface === 'setup' ? ['', 'full'] : ['', 'connection', 'data']).includes(message.section)) return false;
    const open = async () => {
      const base = chromeApi.runtime.getURL(`${message.surface}.html`);
      const contexts = await chromeApi.runtime.getContexts({ contextTypes: ['TAB'] });
      const existing = contexts.find((context) => context.tabId >= 0
        && (context.documentUrl === base || context.documentUrl?.startsWith(`${base}#`)));
      if (existing) {
        await chromeApi.tabs.update(existing.tabId, { active: true,
          ...(message.section ? { url: `${base}#${message.section}` } : {}) });
        await chromeApi.windows.update(existing.windowId, { focused: true });
      } else {
        await chromeApi.tabs.create({ url: base + (message.section ? `#${message.section}` : '') });
      }
    };
    const operation = queue.then(open);
    queue = operation.catch(() => undefined);
    void operation.then(() => reply({ ok: true }), () => reply({ ok: false, code: 'page_unavailable' }));
    return true;
  });
}
