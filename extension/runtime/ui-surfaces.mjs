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

export const DESKTOP_HANDOFF_STORAGE_KEY = 'desktop_handoff_v1';
export const DESKTOP_HANDOFF_WINDOW_KEY = 'desktop_handoff_window_v1';
const SECTIONS = Object.freeze({ setup: ['', 'full', 'desktop'], options: ['', 'connection', 'data', 'history'] });
const WINDOW_SIZE = Object.freeze({ width: 480, height: 760 });

async function centredOn(chromeApi, windowId) {
  try {
    const anchor = await chromeApi.windows.get(windowId);
    if (![anchor.left, anchor.top, anchor.width, anchor.height].every(Number.isInteger)) return {};
    return {
      left: Math.max(0, anchor.left + Math.round((anchor.width - WINDOW_SIZE.width) / 2)),
      top: Math.max(0, anchor.top + Math.round((anchor.height - WINDOW_SIZE.height) / 2)),
    };
  } catch { return {}; }
}

// Reuse an existing page. Only windows created here acquire completion ownership.
export async function openSurfacePage(chromeApi, { surface, section = '', presentation = 'tab', anchorTab = null }) {
  if (!Object.hasOwn(SECTIONS, surface) || !SECTIONS[surface].includes(section)) throw new Error('page_unavailable');
  const base = chromeApi.runtime.getURL(`${surface}.html`);
  const url = base + (section ? `#${section}` : '');
  if (presentation === 'window' && anchorTab !== null) {
    await chromeApi.storage.session.set({
      [DESKTOP_HANDOFF_STORAGE_KEY]: { tab_id: anchorTab.id, window_id: anchorTab.windowId },
    });
  }
  const contexts = await chromeApi.runtime.getContexts({ contextTypes: ['TAB'] });
  const existing = contexts.find((context) => context.tabId >= 0
    && (context.documentUrl === base || context.documentUrl?.startsWith(`${base}#`)));
  if (existing) {
    await chromeApi.tabs.update(existing.tabId, { active: true, ...(section ? { url } : {}) });
    if (presentation === 'window' && section) {
      const saved = (await chromeApi.storage.session.get?.([DESKTOP_HANDOFF_WINDOW_KEY]))?.[DESKTOP_HANDOFF_WINDOW_KEY];
      if (saved?.window_id === existing.windowId && saved.url === existing.documentUrl) {
        await chromeApi.storage.session.set({ [DESKTOP_HANDOFF_WINDOW_KEY]: { window_id: existing.windowId, url } });
      }
    }
    await chromeApi.windows.update(existing.windowId, { focused: true });
  } else if (presentation === 'window') {
    const opened = await chromeApi.windows.create({
      url, type: 'popup', focused: true, ...WINDOW_SIZE,
      ...(anchorTab ? await centredOn(chromeApi, anchorTab.windowId) : {}),
    });
    if (Number.isInteger(opened?.id)) await chromeApi.storage.session.set({
      [DESKTOP_HANDOFF_WINDOW_KEY]: { window_id: opened.id, url },
    });
  } else {
    await chromeApi.tabs.create({ url });
  }
}

// One worker serializes opens from all extension pages and the desktop port.
export function createSurfaceOpener(chromeApi = globalThis.chrome) {
  let queue = Promise.resolve();
  return (request) => {
    const operation = queue.then(() => openSurfacePage(chromeApi, request));
    queue = operation.catch(() => undefined);
    return operation;
  };
}

export function registerSurfaceNavigation(chromeApi = globalThis.chrome, open = createSurfaceOpener(chromeApi)) {
  chromeApi.runtime.onMessage.addListener((message, sender, reply) => {
    if (message?.type !== UI_OPEN_SURFACE_MESSAGE_TYPE || !uiSurface(sender, chromeApi)) return false;
    if (Object.keys(message).sort().join(',') !== 'section,surface,type'
      || !['setup', 'options'].includes(message.surface)
      || !(message.surface === 'setup' ? ['', 'full'] : ['', 'connection', 'data']).includes(message.section)) return false;
    void open({ surface: message.surface, section: message.section })
      .then(() => reply({ ok: true }), () => reply({ ok: false, code: 'page_unavailable' }));
    return true;
  });
  return open;
}
